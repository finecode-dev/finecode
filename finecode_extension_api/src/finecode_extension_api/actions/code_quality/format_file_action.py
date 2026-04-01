# docs: docs/reference/actions.md
import dataclasses
import sys
from typing import NamedTuple

from finecode_extension_api.interfaces import ifileeditor, ilogger
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


class FileInfo(NamedTuple):
    file_content: str
    file_version: str


FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="FormatFileAction")


@dataclasses.dataclass
class FormatFileCallerRunContextKwargs(code_action.CallerRunContextKwargs):
    """Caller-provided parameters for FormatFileRunContext.

    When ``format_file`` is called from ``format_files``, the parent passes a
    shared file editor session so that all files share one session and each file
    is blocked only for the duration of its own formatting.

    When ``format_file`` is called standalone, no kwargs are
    passed and the context creates its own session.

    When ``format_file`` dispatches to a language-specific subaction, the file
    is already read and blocked by the parent ``format_file`` context. The
    dispatch handler passes both the session and the current ``file_info`` so
    the subaction can reuse that data and skip a redundant read.

    This stays in the generic ``format_file`` context because the reuse
    principle applies to all language-specific subactions, not just one
    language.
    """

    file_editor_session: ifileeditor.IFileEditorSession | None = None
    file_info: FileInfo | None = None


@dataclasses.dataclass
class FormatFileRunPayload(code_action.RunActionPayload):
    file_path: ResourceUri
    save: bool


class FormatFileRunContext(code_action.RunActionContext[FormatFileRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: FormatFileRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
        caller_kwargs: FormatFileCallerRunContextKwargs | None = None,
        partial_result_sender: code_action.PartialResultSender = code_action._NOOP_SENDER,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            partial_result_sender=partial_result_sender,
            progress_sender=progress_sender,
        )
        self._file_editor = file_editor
        self._logger = logger
        self._caller_kwargs = caller_kwargs
        self.file_info: FileInfo
        self.file_editor_session: ifileeditor.IFileEditorSession

    @override
    async def init(self) -> None:
        parent_session = (
            self._caller_kwargs.file_editor_session if self._caller_kwargs else None
        )
        parent_file_info = (
            self._caller_kwargs.file_info if self._caller_kwargs else None
        )
        self._logger.trace(
            f"R{self.run_id} | FormatFileRunContext.init: parent_file_info={'provided' if parent_file_info is not None else 'None (will read & block file)'}"
        )

        if parent_session is not None:
            self.file_editor_session = parent_session
        else:
            self.file_editor_session = await self.exit_stack.enter_async_context(
                self._file_editor.session(FILE_OPERATION_AUTHOR)
            )

        if parent_file_info is not None:
            # file is already read and blocked by the caller (e.g. dispatch
            # handler calling a language subaction)
            self.file_info = parent_file_info
        else:
            # read and block the file for the duration of this context
            file_path = resource_uri_to_path(self.initial_payload.file_path)
            file_info = await self.exit_stack.enter_async_context(
                self.file_editor_session.read_file(file_path, block=True)
            )
            self.file_info = FileInfo(
                file_content=file_info.content,
                file_version=file_info.version,
            )


@dataclasses.dataclass
class FormatFileRunResult(code_action.RunActionResult):
    changed: bool
    code: str

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, FormatFileRunResult):
            return
        if other.changed:
            self.changed = True
            self.code = other.code

    def to_text(self) -> str | textstyler.StyledText:
        return "reformatted" if self.changed else "unchanged"


class FormatFileAction(
    code_action.Action[FormatFileRunPayload, FormatFileRunContext, FormatFileRunResult]
):
    """Format a single file. Item-level action for use by per-file handlers."""

    PAYLOAD_TYPE = FormatFileRunPayload
    RUN_CONTEXT_TYPE = FormatFileRunContext
    RESULT_TYPE = FormatFileRunResult
