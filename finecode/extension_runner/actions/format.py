
import sys
from pathlib import Path

from finecode.extension_runner.interfaces import ifilemanager

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode.extension_runner.code_action import CodeAction, CodeActionConfigType, RunActionContext, RunActionPayload, RunActionResult


class FormatRunPayload(RunActionPayload):
    # `apply_on` can be used as identifier of target file e.g. for caching. But it should never be
    # use reading the file content even using finecode file manager, because formatting can be
    # multi-step and output of previous step is input for the next step without saving permanently
    # in file. Use `apply_on_text` as source of target file.
    file_path: Path


class FormatRunContext(RunActionContext):
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        super().__init__()
        self.file_manager = file_manager
        
        self.file_content: str = ''
        # file version is needed to allow proper(version-specific) caching in action. There are at
        # least 2 solutions:
        # - pass file version as payload
        # - use run-specific action context
        # We use the second one, because the first one would require additional annotation of payload
        # parameters to distinguish between user inputs and values added during run, this would make
        # handling of user inputs more tricky.
        self.file_version: str = ''

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
            self.code = other.code


class CodeFormatAction(CodeAction[CodeActionConfigType, FormatRunPayload, FormatRunContext, FormatRunResult]):
    # format actions can both analyse and modify the code. Analysis is required for example to
    # report errors that cannot be fixed automatically.
    ...
