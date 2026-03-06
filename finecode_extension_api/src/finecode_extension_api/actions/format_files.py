import dataclasses
import sys
from pathlib import Path
from typing import NamedTuple

from finecode_extension_api.interfaces import ifileeditor

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class FormatFilesRunPayload(code_action.RunActionPayload):
    file_paths: list[Path]
    save: bool


class FileInfo(NamedTuple):
    file_content: str
    file_version: str


FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="FormatFilesAction")


class FormatFilesRunContext(code_action.RunActionContext[FormatFilesRunPayload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: FormatFilesRunPayload,
        meta: code_action.RunActionMeta,
        file_editor: ifileeditor.IFileEditor,
        info_provider: code_action.RunContextInfoProvider,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )
        self.file_editor = file_editor

        self.file_info_by_path: dict[Path, FileInfo] = {}
        self.file_editor_session: ifileeditor.IFileEditorSession

    @override
    async def init(self) -> None:
        self.file_editor_session = await self.exit_stack.enter_async_context(
            self.file_editor.session(FILE_OPERATION_AUTHOR)
        )
        for file_path in self.initial_payload.file_paths:
            file_info = await self.exit_stack.enter_async_context(
                self.file_editor_session.read_file(file_path, block=True)
            )
            file_content = file_info.content
            file_version = file_info.version
            self.file_info_by_path[file_path] = FileInfo(
                file_content=file_content, file_version=file_version
            )


@dataclasses.dataclass
class FormatRunFileResult:
    changed: bool
    # changed code or empty string if code was not changed
    code: str


@dataclasses.dataclass
class FormatFilesRunResult(code_action.RunActionResult):
    result_by_file_path: dict[Path, FormatRunFileResult]

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

        for file_path, file_result in self.result_by_file_path.items():
            if file_result.changed:
                text.append("reformatted ")
                text.append_styled(file_path.as_posix(), bold=True)
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
    PAYLOAD_TYPE = FormatFilesRunPayload
    RUN_CONTEXT_TYPE = FormatFilesRunContext
    RESULT_TYPE = FormatFilesRunResult
