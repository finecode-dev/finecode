from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol


class IProcess(Protocol):
    def get_exit_code(self) -> int | None: ...

    def get_output(self) -> str: ...

    def get_error_output(self) -> str: ...

    def write_to_stdin(self, value: str) -> None: ...

    def close_stdin(self) -> None: ...


class ISyncProcess(IProcess, Protocol):
    def wait_for_end(self, timeout: float | None = None) -> None: ...


class IAsyncProcess(IProcess, Protocol):
    async def wait_for_end(self, timeout: float | None = None) -> None: ...

    def stdout_lines(self) -> AsyncIterator[str]:
        """Consume stdout line by line as the process produces it.

        Yields lines with the trailing newline stripped, and finishes when the
        stream reaches EOF -- which may be before the process exits. Lines
        produced between spawn and this call are replayed first, so nothing is
        lost by subscribing late.

        Subscribing switches this stream out of accumulating mode:
        `get_output()` raises afterwards, and only one subscriber per stream is
        allowed. Streaming is offered only on the async flavour -- the sync one
        has no way to interleave reads with anything else.

        Raises `RuntimeError` if the stream ended in an unrecoverable error
        (a line past the reader's size limit, or output that is not UTF-8)
        rather than at a genuine EOF. Abandoning the iterator early is fine and
        drops the stream's remaining output; the process keeps being drained.
        """
        ...

    def stderr_lines(self) -> AsyncIterator[str]:
        """Consume stderr line by line. See `stdout_lines`.

        The two streams decide independently, so a caller may stream stdout
        while stderr keeps accumulating for a failure message.
        """
        ...


class ICommandRunner(Protocol):
    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> IAsyncProcess: ...

    def run_sync(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> ISyncProcess: ...
