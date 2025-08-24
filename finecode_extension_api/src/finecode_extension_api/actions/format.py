import dataclasses
import sys
from pathlib import Path
from typing import NamedTuple

from finecode_extension_api.interfaces import ifilemanager

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class FormatRunPayload(code_action.RunActionPayload):
    file_paths: list[Path]
    save: bool


class FileInfo(NamedTuple):
    file_content: str
    file_version: str


class FormatRunContext(code_action.RunActionContext):
    def __init__(
        self,
        run_id: int,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__(run_id=run_id)
        self.file_manager = file_manager

        self.file_info_by_path: dict[Path, FileInfo] = {}

    async def init(self, initial_payload: FormatRunPayload) -> None:
        for file_path in initial_payload.file_paths:
            file_content = await self.file_manager.get_content(file_path)
            file_version = await self.file_manager.get_file_version(file_path)
            self.file_info_by_path[file_path] = FileInfo(
                file_content=file_content, file_version=file_version
            )


@dataclasses.dataclass
class FormatRunFileResult:
    changed: bool
    # changed code or empty string if code was not changed
    code: str


@dataclasses.dataclass
class FormatRunResult(code_action.RunActionResult):
    result_by_file_path: dict[Path, FormatRunFileResult]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, FormatRunResult):
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
                text.append_styled(file_path, bold=True)
                text.append("\n")
            else:
                unchanged_counter += 1
        text.append_styled(
            f"{unchanged_counter} files", foreground=textstyler.Color.BLUE
        )
        text.append(" unchanged.")

        return text


class FormatAction(code_action.Action):
    PAYLOAD_TYPE = FormatRunPayload
    RUN_CONTEXT_TYPE = FormatRunContext
    RESULT_TYPE = FormatRunResult


@dataclasses.dataclass
class SaveFormatHandlerConfig(code_action.ActionHandlerConfig): ...


class SaveFormatHandler(
    code_action.ActionHandler[FormatAction, SaveFormatHandlerConfig]
):
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        self.file_manager = file_manager

    async def run(
        self, payload: FormatRunPayload, run_context: FormatRunContext
    ) -> FormatRunResult:
        file_paths = payload.file_paths
        save = payload.save

        if save is True:
            for file_path in file_paths:
                file_content = run_context.file_info_by_path[file_path].file_content
                await self.file_manager.save_file(
                    file_path=file_path, file_content=file_content
                )

        result = FormatRunResult(
            result_by_file_path={
                file_path: FormatRunFileResult(
                    changed=False,
                    code=run_context.file_info_by_path[file_path].file_content,
                )
                for file_path in file_paths
            }
        )
        return result
