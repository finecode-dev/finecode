from __future__ import annotations

import collections.abc
import dataclasses
import typing
from typing import Any, Awaitable, Callable

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner


PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class ProjectActionRunnerImpl(iprojectactionrunner.IProjectActionRunner):
    """Calls the WM back-channel finecode/runActionInProject."""

    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def run_action(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> ResultT:
        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
        raw_result = await self._send(
            "finecode/runActionInProject",
            {
                "actionSource": action_source,
                "payload": dataclasses.asdict(payload),
                "meta": {
                    "trigger": meta.trigger.value,
                    "devEnv": meta.dev_env.value,
                    "orchestrationDepth": meta.orchestration_depth,
                },
            },
        )
        return action_type.RESULT_TYPE(**raw_result["result"])  # type: ignore[attr-defined]

    def run_action_iter(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> collections.abc.AsyncIterator[ResultT]:
        raise NotImplementedError(
            "run_action_iter is not supported for project-scope execution"
        )
