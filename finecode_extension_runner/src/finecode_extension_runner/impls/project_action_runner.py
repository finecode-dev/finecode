from __future__ import annotations

import collections.abc
import dataclasses
import typing
from typing import Any, Awaitable, Callable

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iactionrunner, iprojectactionrunner
from finecode_extension_runner import run_utils

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class ProjectActionRunnerImpl(iprojectactionrunner.IProjectActionRunner):
    """Calls the WM back-channel finecode/runActionInProject."""

    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def run_action(
        self,
        action: iactionrunner.ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> ResultT:
        action_source: str = action.source  # type: ignore[attr-defined]
        action_cls = run_utils.import_module_member_by_source_str(action_source)
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
        result_dict = raw_result.get("result", {}) or {}
        return action_cls.RESULT_TYPE(**result_dict)

    def run_action_iter(
        self,
        action: iactionrunner.ActionDeclaration[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
    ) -> collections.abc.AsyncIterator[ResultT]:
        raise NotImplementedError(
            "run_action_iter is not supported for project-scope execution"
        )
