from __future__ import annotations

import dataclasses
import pathlib
import typing
from typing import Any, Awaitable, Callable

import apischema
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iworkspaceactionrunner

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class WorkspaceActionRunnerImpl(iworkspaceactionrunner.IWorkspaceActionRunner):
    """Calls the WM back-channel finecode/runActionInWorkspace."""

    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def run_action_in_projects(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, ResultT]:
        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
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
                "concurrently": concurrently,
            },
        )
        results_by_project: dict = raw["resultsByProject"]
        return {
            pathlib.Path(k): apischema.deserialize(
                action_type.RESULT_TYPE, next(iter(v.values()), {})  # type: ignore[attr-defined]
            )
            for k, v in results_by_project.items()
        }
