"""Stubs for the sync_python_interpreters tests.

The deriving handler asks the provisioner what it can obtain. In tests that answer is
stubbed rather than shelled out to uv, so the tests stay hermetic and do not depend on
which interpreters this machine's uv happens to offer.
"""

import dataclasses

from fine_envs.list_obtainable_toolchains_action import (
    ListObtainableToolchainsRunResult,
)
from fine_python_lang import list_obtainable_python_interpreters_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ifileeditor,
    ilogger,
    iprojectinfoprovider,
)

OBTAINABLE = [
    "cpython@3.10",
    "cpython@3.11",
    "cpython@3.12",
    "cpython@3.13",
    "cpython@3.14",
    "pypy@3.11",
    "graalpy@3.12",
]


@dataclasses.dataclass
class StubObtainableInterpretersHandlerConfig(code_action.ActionHandlerConfig): ...


class StubObtainableInterpretersHandler(
    code_action.ActionHandler[
        list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersAction,
        StubObtainableInterpretersHandlerConfig,
    ]
):
    async def run(
        self,
        payload: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunPayload,
        run_context: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunContext,
    ) -> ListObtainableToolchainsRunResult:
        return ListObtainableToolchainsRunResult(toolchains=list(OBTAINABLE))


CONCURRENT_EDIT_MARKER = "# edited while sync_toolchains was running\n"


@dataclasses.dataclass
class ConcurrentEditObtainableHandlerConfig(code_action.ActionHandlerConfig): ...


class ConcurrentEditObtainableHandler(
    code_action.ActionHandler[
        list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersAction,
        ConcurrentEditObtainableHandlerConfig,
    ]
):
    """Answers like StubObtainableInterpretersHandler, but first edits the project
    definition file through the file editor.

    The sync asks for obtainable interpreters after reading the file and before
    writing it, so this simulates another writer (e.g. the IDE) changing the file
    while the sync is in flight.
    """

    def __init__(
        self,
        file_editor: ifileeditor.IFileEditor,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.file_editor = file_editor
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunPayload,
        run_context: list_obtainable_python_interpreters_action.ListObtainablePythonInterpretersRunContext,
    ) -> ListObtainableToolchainsRunResult:
        project_def_path = self.project_info_provider.get_current_project_def_path()
        async with self.file_editor.session(
            author=ifileeditor.FileOperationAuthor(id="concurrent-editor")
        ) as session:
            async with session.read_file(project_def_path) as file_info:
                content = file_info.content
            await session.save_file(
                file_path=project_def_path,
                file_content=CONCURRENT_EDIT_MARKER + content,
            )
        return ListObtainableToolchainsRunResult(toolchains=list(OBTAINABLE))


class CollectingLogger(ilogger.ILogger):
    """ILogger that keeps what it was told, for tests that assert on a report.

    The runner's own NoOpLogger discards everything, so a warning the handler emits as
    its only signal would be untestable through it.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def exception(self, exception: Exception) -> None:
        pass

    def trace(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        pass

    def disable(self, package: str) -> None:
        pass

    def enable(self, package: str) -> None:
        pass
