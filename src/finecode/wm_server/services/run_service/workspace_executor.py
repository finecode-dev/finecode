from __future__ import annotations

import pathlib
import typing

from finecode.wm_server import context
from finecode.wm_server.runner import elicitation_bridge
from finecode.wm_server.runner.runner_client import (
    DevEnv,
    RunActionResponse,
    RunActionTrigger,
    RunResultFormat,
)
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.execution_scopes import (
    DEFAULT_ORCHESTRATION_POLICY,
    OrchestrationPolicy,
)


class WorkspaceExecutor:
    """Fan-out an action across multiple projects.

    Wraps proxy_utils.run_actions_in_projects() with a fan-out cap guard.
    actions_by_project uses action names (not sources) because workspace fan-out
    originates from external API calls which are name-centric.
    """

    def __init__(self, ws_context: context.WorkspaceContext) -> None:
        self._ws_context = ws_context

    async def run_actions_in_projects(
        self,
        actions_by_project: dict[pathlib.Path, list[str]],
        params: dict[str, typing.Any],
        run_trigger: RunActionTrigger,
        dev_env: DevEnv,
        orchestration_depth: int = 0,
        policy: OrchestrationPolicy = DEFAULT_ORCHESTRATION_POLICY,
        concurrently: bool = True,
        result_formats: list[RunResultFormat] | None = None,
        payload_overrides_by_project: dict[str, dict[str, typing.Any]] | None = None,
        progress_token_by_project: dict[pathlib.Path, dict[str, str]] | None = None,
        cancellable: bool = False,
        *,
        origin: elicitation_bridge.RunDispatchOrigin | None,
    ) -> dict[pathlib.Path, dict[str, RunActionResponse]]:
        # `max_project_fanout` is a runaway-orchestration guard (ADR-0016), not a
        # capacity limit — it bounds the blast radius of an action whose handler
        # fans out, whose handlers fan out again. It therefore applies only to
        # nested orchestration. At depth 0 the fan-out width is whatever the
        # workspace happens to contain, chosen by a person, not by a recursion:
        # a 100-project workspace is not a runaway loop, and refusing it makes
        # every workspace-wide action unusable past an arbitrary size. Width at
        # depth 0 is handled by throttling (the semaphore in
        # proxy_utils.run_actions_in_projects), not by refusal.
        if (
            orchestration_depth > 0
            and len(actions_by_project) > policy.max_project_fanout
        ):
            raise ActionRunFailed(
                f"Workspace fan-out {len(actions_by_project)} exceeds limit "
                f"{policy.max_project_fanout} at orchestration depth "
                f"{orchestration_depth}"
            )

        _result_formats = (
            result_formats
            if result_formats is not None
            else [proxy_utils.RunResultFormat.JSON]
        )

        return await proxy_utils.run_actions_in_projects(
            actions_by_project=actions_by_project,
            action_payload=params,
            ws_context=self._ws_context,
            concurrently=concurrently,
            result_formats=_result_formats,
            run_trigger=run_trigger,
            dev_env=dev_env,
            payload_overrides_by_project=payload_overrides_by_project,
            progress_token_by_project=progress_token_by_project,
            orchestration_depth=orchestration_depth,
            cancellable=cancellable,
            origin=origin,
        )
