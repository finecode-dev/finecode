from pathlib import Path
from typing import Protocol


class ICommandRunner(Protocol):
    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ): ...
