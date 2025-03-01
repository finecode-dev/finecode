import sys
from pathlib import Path
from typing import NamedTuple

from finecode.extension_runner.interfaces import ifilemanager

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode.extension_runner.code_action import (
    CodeAction,
    CodeActionConfig,
    CodeActionConfigType,
    RunActionContext,
    RunActionPayload,
    RunActionResult,
)


class FormatRunPayload(RunActionPayload):
    # `apply_on` can be used as identifier of target file e.g. for caching. But it should never be
    # use reading the file content even using finecode file manager, because formatting can be
    # multi-step and output of previous step is input for the next step without saving permanently
    # in file. Use `apply_on_text` as source of target file.
    file_path: Path
    save: bool


class FormatRunContext(RunActionContext):
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__()
        self.file_manager = file_manager

        self.file_content: str = ""
        # file version is needed to allow proper(version-specific) caching in action. There are at
        # least 2 solutions:
        # - pass file version as payload
        # - use run-specific action context
        # We use the second one, because the first one would require additional annotation of payload
        # parameters to distinguish between user inputs and values added during run, this would make
        # handling of user inputs more tricky.
        self.file_version: str = ""

    async def init(self, initial_payload: FormatRunPayload) -> None:
        file_path = initial_payload.file_path
        self.file_content = await self.file_manager.get_content(file_path)
        self.file_version = await self.file_manager.get_file_version(file_path)


class FormatRunResult(RunActionResult):
    changed: bool
    # changed code or empty string if code was not changed
    code: str

    @override
    def update(self, other: RunActionResult) -> None:
        if not isinstance(other, FormatRunResult):
            return
        if other.changed is True:
            self.changed = True
            self.code = other.code


class FormatCodeAction(
    CodeAction[
        CodeActionConfigType, FormatRunPayload, FormatRunContext, FormatRunResult
    ]
):
    # format actions can both analyse and modify the code. Analysis is required for example to
    # report errors that cannot be fixed automatically.
    ...


class FormatSaveInPlaceCodeActionConfig(CodeActionConfig): ...


class FormatSaveInPlaceCodeAction(FormatCodeAction[FormatSaveInPlaceCodeActionConfig]):
    def __init__(
        self,
        config: FormatSaveInPlaceCodeActionConfig,
        context: FormatRunContext,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__(config=config, context=context)
        self.file_manager = file_manager

    async def run(
        self, payload: FormatRunPayload, run_context: FormatRunContext
    ) -> FormatRunResult:
        file_path = payload.file_path
        file_content = run_context.file_content
        save = payload.save

        if save is True:
            await self.file_manager.save_file(
                file_path=file_path, file_content=file_content
            )

        result = FormatRunResult(changed=False, code=file_content)
        return result


class FormatManyRunPayload(RunActionPayload):
    file_paths: list[Path]
    save: bool


class FileInfo(NamedTuple):
    file_content: str
    file_version: str


class FormatManyRunContext(RunActionContext):
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


class FormatManyRunResult(RunActionResult):
    result_by_file_path: dict[Path, FormatRunResult]

    @override
    def update(self, other: RunActionResult) -> None:
        if not isinstance(other, FormatManyRunResult):
            return

        for file_path, other_result in other.result_by_file_path.items():
            if other_result.changed is True:
                self.result_by_file_path[file_path] = other_result


class FormatManyCodeAction(
    CodeAction[
        CodeActionConfigType,
        FormatManyRunPayload,
        RunActionContext,
        FormatManyRunResult,
    ]
): ...


class FormatManySaveInPlaceCodeActionConfig(CodeActionConfig): ...


class FormatManySaveInPlaceCodeAction(
    FormatManyCodeAction[FormatSaveInPlaceCodeActionConfig]
):
    def __init__(
        self,
        config: FormatSaveInPlaceCodeActionConfig,
        context: FormatRunContext,
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
