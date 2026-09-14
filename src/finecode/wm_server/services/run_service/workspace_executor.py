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

    Wraps proxy_utils.run_actions_in_projects() with a recursion-depth guard.
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
        # Recursion is bounded by height, not width. A nested fan-out's width can
        # never exceed the workspace's project count (er_dispatch validates every
        # requested path), so a width cap measures the workspace, not a runaway:
        # it could never fire at <= cap projects and refused every legitimate
        # workspace-wide gather above it (ADR-0095). Subprocess width is bounded
        # at the leaf by the process budget (ADR-0090).
        if orchestration_depth >= policy.max_recursion_depth:
            raise ActionRunFailed(
                f"Orchestration depth {orchestration_depth} reached limit "
                f"{policy.max_recursion_depth}. Actions: "
                f"{sorted({a for names in actions_by_project.values() for a in names})}"
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
