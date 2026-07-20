"""Stubs for the sync_python_interpreters tests.

The deriving handler asks the provisioner what it can obtain. In tests that answer is
stubbed rather than shelled out to uv, so the tests stay hermetic and do not depend on
which interpreters this machine's uv happens to offer.
"""

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger
from fine_envs.list_obtainable_toolchains_action import (
    ListObtainableToolchainsRunResult,
)
from fine_python_lang import list_obtainable_python_interpreters_action

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
