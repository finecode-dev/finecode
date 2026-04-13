from __future__ import annotations

import collections.abc
import contextlib
import pathlib
import typing

from finecode.wm_server import context, domain
from finecode.wm_server.runner.runner_client import (
    RunActionTrigger,
    DevEnv,
    RunResultFormat,
    RunActionResponse,
)
from finecode.wm_server.services.run_service import proxy_utils
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.execution_scopes import (
    OrchestrationPolicy,
    DEFAULT_ORCHESTRATION_POLICY,
)


class ProjectExecutor:
    """Execute an action at project scope.

    Wraps proxy_utils.run_action() with:
    - source → name resolution via project.actions
    - recursion-depth guard (raises ActionRunFailed before contacting any runner)
    """

    def __init__(self, ws_context: context.WorkspaceContext) -> None:
        self._ws_context = ws_context

    def _resolve_action_name(
        self, action_source: str, project: domain.CollectedProject
    ) -> str:
        # Use canonical_source (resolved by ER)
        try:
            return next(
                a.name for a in project.actions
                if a.canonical_source == action_source
            )
        except StopIteration:
            raise ActionRunFailed(
                f"No action with source '{action_source}' found in project {project.dir_path}"
            )

    async def run_action(
        self,
        action_source: str,
        params: dict[str, typing.Any],
        project_path: pathlib.Path,
        run_trigger: RunActionTrigger,
        dev_env: DevEnv,
        orchestration_depth: int = 0,
        policy: OrchestrationPolicy = DEFAULT_ORCHESTRATION_POLICY,
        result_formats: list[RunResultFormat] | None = None,
        progress_token: int | str | None = None,
        initialize_all_handlers: bool = False,
    ) -> RunActionResponse:
        if orchestration_depth >= policy.max_recursion_depth:
            raise ActionRunFailed(
                f"Orchestration depth {orchestration_depth} reached limit "
                f"{policy.max_recursion_depth}. Action source: {action_source}"
            )

        project = self._ws_context.ws_projects.get(project_path)
        if not isinstance(project, domain.CollectedProject):
            raise ActionRunFailed(
                f"Project {project_path} has no valid config"
            )

        action_name = self._resolve_action_name(action_source, project)

        return await proxy_utils.run_action(
            action_name=action_name,
            params=params,
            project_def=project,
            ws_context=self._ws_context,
            run_trigger=run_trigger,
            dev_env=dev_env,
            result_formats=result_formats,
            initialize_all_handlers=initialize_all_handlers,
            progress_token=progress_token,
            orchestration_depth=orchestration_depth + 1,
        )

    @contextlib.asynccontextmanager
    async def run_action_with_partial_results(
        self,
        action_source: str,
        params: dict[str, typing.Any],
        project_path: pathlib.Path,
        partial_result_token: int | str,
        run_trigger: RunActionTrigger,
        dev_env: DevEnv,
        orchestration_depth: int = 0,
        policy: OrchestrationPolicy = DEFAULT_ORCHESTRATION_POLICY,
        result_formats: list[RunResultFormat] | None = None,
        progress_token: int | str | None = None,
    ) -> collections.abc.AsyncIterator[proxy_utils.RunWithPartialResultsContext]:
        if orchestration_depth >= policy.max_recursion_depth:
            raise ActionRunFailed(
                f"Orchestration depth {orchestration_depth} reached limit "
                f"{policy.max_recursion_depth}. Action source: {action_source}"
            )

        project = self._ws_context.ws_projects.get(project_path)
        if not isinstance(project, domain.CollectedProject):
            raise ActionRunFailed(
                f"Project {project_path} has no valid config"
            )

        action_name = self._resolve_action_name(action_source, project)

        async with proxy_utils.run_with_partial_results(
            action_name=action_name,
            params=params,
            partial_result_token=partial_result_token,
            project_dir_path=project_path,
            run_trigger=run_trigger,
            dev_env=dev_env,
            ws_context=self._ws_context,
            result_formats=result_formats,
            progress_token=progress_token,
        ) as ctx:
            yield ctx
