# docs: docs/reference/actions.md
import collections.abc
import dataclasses
import sys

from finecode_extension_api.interfaces import (
    ifileeditor,  # used in FormatFilesRunContext
)
from finecode_extension_api.resource_uri import ResourceUri

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.code_quality.format_file_action import (
    FILE_OPERATION_AUTHOR,
)


@dataclasses.dataclass
class FormatFilesRunPayload(
    code_action.RunActionPayload, collections.abc.AsyncIterable[ResourceUri]
):
    file_paths: list[ResourceUri]
    save: bool

    def __aiter__(self) -> collections.abc.AsyncIterator[ResourceUri]:
        return FormatFilesRunPayloadIterator(self)


@dataclasses.dataclass
class FormatFilesRunPayloadIterator(collections.abc.AsyncIterator[ResourceUri]):
    def __init__(self, payload: FormatFilesRunPayload) -> None:
        self._payload = payload
        self._index = 0

    def __aiter__(self) -> "FormatFilesRunPayloadIterator":
        return self

    async def __anext__(self) -> ResourceUri:
        if self._index >= len(self._payload.file_paths):
            raise StopAsyncIteration()
        result = self._payload.file_paths[self._index]
        self._index += 1
        return result


class FormatFilesRunContext(
    code_action.RunActionWithPartialResultsContext[FormatFilesRunPayload]
):
    def __init__(
        self,
        run_id: int,
        initial_payload: FormatFilesRunPayload,
        meta: code_action.RunActionMeta,
        file_editor: ifileeditor.IFileEditor,
        info_provider: code_action.RunContextInfoProvider,
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
        self.file_editor = file_editor
        self.file_editor_session: ifileeditor.IFileEditorSession

    @override
    async def init(self) -> None:
        self.file_editor_session = await self.exit_stack.enter_async_context(
            self.file_editor.session(FILE_OPERATION_AUTHOR)
        )


@dataclasses.dataclass
class FormatRunFileResult:
    changed: bool
    # changed code or empty string if code was not changed
    code: str


@dataclasses.dataclass
class FormatFilesRunResult(code_action.RunActionResult):
    result_by_file_path: dict[ResourceUri, FormatRunFileResult]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, FormatFilesRunResult):
            return

        for file_path, other_result in other.result_by_file_path.items():
            if other_result.changed is True:
                self.result_by_file_path[file_path] = other_result

    def to_text(self) -> str | textstyler.StyledText:
        text: textstyler.StyledText = textstyler.StyledText()
        unchanged_counter: int = 0

        for file_uri, file_result in self.result_by_file_path.items():
            if file_result.changed:
                text.append("reformatted ")
                text.append_styled(file_uri, bold=True)
                text.append("\n")
            else:
                unchanged_counter += 1
        text.append_styled(
            f"{unchanged_counter} files", foreground=textstyler.Color.BLUE
        )
        text.append(" unchanged.")

        return text


class FormatFilesAction(
    code_action.Action[
        FormatFilesRunPayload, FormatFilesRunContext, FormatFilesRunResult
    ]
):
    """Format specific files. Internal action dispatched by format."""

    PAYLOAD_TYPE = FormatFilesRunPayload
    RUN_CONTEXT_TYPE = FormatFilesRunContext
    RESULT_TYPE = FormatFilesRunResult
