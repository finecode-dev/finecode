from pathlib import Path
from typing import Any, TypeAlias

from finecode_extension_api.interfaces import iactionrunner


class ActionRunner(iactionrunner.IActionRunner):
    def __init__(self, internal_service_func):
        self._internal_service_func = internal_service_func

    async def run_action(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._internal_service_func(action_name=name, payload=payload)
