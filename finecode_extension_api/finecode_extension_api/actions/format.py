import sys
from pathlib import Path
from typing import NamedTuple

from finecode_extension_api.interfaces import ifilemanager

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import code_action


class FormatManyRunPayload(code_action.RunActionPayload):
    file_paths: list[Path]
    save: bool


class FileInfo(NamedTuple):
    file_content: str
    file_version: str


class FormatManyRunContext(code_action.RunActionContext):
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__()
        self.file_manager = file_manager

        self.file_info_by_path: dict[Path, FileInfo] = {}

    async def init(self, initial_payload: FormatManyRunPayload) -> None:
        for file_path in initial_payload.file_paths:
            file_content = await self.file_manager.get_content(file_path)
            file_version = await self.file_manager.get_file_version(file_path)
            self.file_info_by_path[file_path] = FileInfo(
                file_content=file_content, file_version=file_version
            )


class FormatRunResult(code_action.RunActionResult):
    changed: bool
    # changed code or empty string if code was not changed
    code: str

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, FormatRunResult):
            return
        if other.changed is True:
            self.changed = True
            self.code = other.code


class FormatManyRunResult(code_action.RunActionResult):
    result_by_file_path: dict[Path, FormatRunResult]

    @override
    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, FormatManyRunResult):
            return

        for file_path, other_result in other.result_by_file_path.items():
            if other_result.changed is True:
                self.result_by_file_path[file_path] = other_result


class FormatManyCodeAction(
    code_action.CodeAction[
        code_action.CodeActionConfigType,
        FormatManyRunPayload,
        code_action.RunActionContext,
        FormatManyRunResult,
    ]
): ...


class FormatManySaveInPlaceCodeActionConfig(code_action.CodeActionConfig): ...


class FormatManySaveInPlaceCodeAction(
    FormatManyCodeAction[FormatManySaveInPlaceCodeActionConfig]
):
    def __init__(
        self,
        config: FormatManySaveInPlaceCodeActionConfig,
        context: code_action.ActionContext,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__(config=config, context=context)
        self.file_manager = file_manager

    async def run(
        self, payload: FormatManyRunPayload, run_context: FormatManyRunContext
    ) -> FormatManyRunResult:
        file_paths = payload.file_paths
        save = payload.save

        if save is True:
            for file_path in file_paths:
                file_content = run_context.file_info_by_path[file_path].file_content
                await self.file_manager.save_file(
                    file_path=file_path, file_content=file_content
                )

        result = FormatManyRunResult(
            result_by_file_path={
                file_path: FormatRunResult(
                    changed=False,
                    code=run_context.file_info_by_path[file_path].file_content,
                )
                for file_path in file_paths
            }
        )
        return result
