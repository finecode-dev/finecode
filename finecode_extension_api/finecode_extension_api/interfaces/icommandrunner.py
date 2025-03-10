from pathlib import Path
from typing import Protocol


class IProcess(Protocol):
    def get_exit_code(self) -> int | None: ...

    def get_output(self) -> str: ...

    def get_error_output(self) -> str: ...


class ISyncProcess(IProcess):
    def wait_for_end(self, timeout: float | None = None) -> None: ...


class IAsyncProcess(IProcess):
    async def wait_for_end(self, timeout: float | None = None) -> None: ...


class ICommandRunner(Protocol):
    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> IAsyncProcess: ...

    def run_sync(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> ISyncProcess: ...
