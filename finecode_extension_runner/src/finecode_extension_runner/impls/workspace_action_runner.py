from __future__ import annotations

import dataclasses
import pathlib
import typing
from typing import Any, Awaitable, Callable

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iactionrunner, iworkspaceactionrunner
from finecode_extension_runner import run_utils

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class WorkspaceActionRunnerImpl(iworkspaceactionrunner.IWorkspaceActionRunner):
    """Calls the WM back-channel finecode/runActionInWorkspace."""

    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def run_action_in_projects(
        self,
        action: iactionrunner.ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
    ) -> dict[pathlib.Path, ResultT]:
        action_source: str = action.source
        action_cls = run_utils.import_module_member_by_source_str(action_source)
        raw = await self._send(
            "finecode/runActionInWorkspace",
            {
                "actionSource": action_source,
                "payload": dataclasses.asdict(payload),
                "meta": {
                    "trigger": meta.trigger.value,
                    "devEnv": meta.dev_env.value,
                    "orchestrationDepth": meta.orchestration_depth,
                },
                "projectPaths": [p.as_posix() for p in project_paths]
                if project_paths is not None
                else None,
            },
        )
        return {
            pathlib.Path(k): action_cls.RESULT_TYPE(**v)
            for k, v in raw.get("resultsByProject", {}).items()
        }
