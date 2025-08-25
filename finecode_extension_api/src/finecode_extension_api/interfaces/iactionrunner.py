from pathlib import Path
from typing import Any, Protocol


class IActionRunner(Protocol):
    async def run_action(
        self, name: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...


class BaseRunActionException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class ActionNotFound(BaseRunActionException): ...


class InvalidActionRunPayload(BaseRunActionException): ...


class ActionRunFailed(BaseRunActionException): ...
