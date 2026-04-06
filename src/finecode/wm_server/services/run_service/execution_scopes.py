from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import pathlib
import typing

from finecode.wm_server.runner.runner_client import (
    RunActionTrigger,
    DevEnv,
    RunResultFormat,
    RunActionResponse,
)
from finecode.wm_server.services.run_service.proxy_utils import RunWithPartialResultsContext


@dataclasses.dataclass
class OrchestrationPolicy:
    max_recursion_depth: int = 8
    max_project_fanout: int = 64


DEFAULT_ORCHESTRATION_POLICY = OrchestrationPolicy()


class IProjectExecutionScope(typing.Protocol):
    """WM-internal contract for project-scope action execution.

    action_source is the action class import path (e.g.
    "finecode_extension_api.actions.lint.LintAction").
    The executor resolves it to an action name via domain.Action.source.
    """

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
    ) -> RunActionResponse: ...

    @contextlib.asynccontextmanager
    def run_action_with_partial_results(
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
    ) -> collections.abc.AsyncIterator[RunWithPartialResultsContext]: ...


class IWorkspaceExecutionScope(typing.Protocol):
    """WM-internal contract for workspace-scope fan-out.

    actions_by_project uses action names (not sources) because workspace fan-out
    originates from external API calls which are name-centric.
    """

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
    ) -> dict[pathlib.Path, dict[str, RunActionResponse]]: ...
