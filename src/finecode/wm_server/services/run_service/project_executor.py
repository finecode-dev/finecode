from __future__ import annotations

import asyncio
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

    def _find_action_name(
        self, action_source: str, project: domain.CollectedProject
    ) -> str | None:
        return next(
            (a.name for a in project.actions if a.canonical_source == action_source),
            None,
        )

    async def _resolve_action_name(
        self, action_source: str, project: domain.CollectedProject
    ) -> str:
        # action_source is always canonical here: ER-initiated calls pass
        # canonical_source directly (derived from cls.__module__.__qualname__).
        action_name = self._find_action_name(action_source, project)
        if action_name is None:
            # A handler may dynamically invoke an action whose own env was never
            # started by the top-level request (e.g. check_imports' dispatch
            # handler calling get_src_artifact_language, which lives in
            # "dev_no_runtime" while dispatch itself runs in "dev_workspace").
            # canonical_source for such actions is only populated once their env
            # has started and reported back via update_runner_config, so give
            # every not-yet-resolved action in this project a chance to resolve
            # before giving up. One action's metadata being unresolvable must not
            # cancel another's resolution that is about to succeed, so gather
            # (not TaskGroup) with return_exceptions=True.
            unresolved = [a for a in project.actions if a.canonical_source is None]
            if unresolved:
                await asyncio.gather(
                    *(
                        proxy_utils.ensure_action_metadata(a, project, self._ws_context)
                        for a in unresolved
                    ),
                    return_exceptions=True,
                )
                action_name = self._find_action_name(action_source, project)

        if action_name is None:
            raise ActionRunFailed(
                f"No action with canonical source '{action_source}' found in project {project.dir_path}"
            )
        return action_name

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
        caller_kwargs: dict | None = None,
        allow_no_handlers: bool = False,
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

        action_name = await self._resolve_action_name(action_source, project)

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
            caller_kwargs=caller_kwargs,
            allow_no_handlers=allow_no_handlers,
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
        caller_kwargs: dict | None = None,
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

        action_name = await self._resolve_action_name(action_source, project)

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
            caller_kwargs=caller_kwargs,
        ) as ctx:
            yield ctx
