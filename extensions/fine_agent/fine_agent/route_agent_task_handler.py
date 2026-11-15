"""Role router for agent tasks."""

from __future__ import annotations

import dataclasses
import time
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iprojectinfoprovider,
)

from fine_agent import backend_support
from fine_agent.history_types import AgentBackend
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunContext,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from fine_agent.session_support import (
    session_backend_error,
    session_id_error,
    session_project_error,
)

__all__ = [
    "AgentTaskRoute",
    "RouteAgentTaskHandler",
    "RouteAgentTaskHandlerConfig",
]


@dataclasses.dataclass
class AgentTaskRoute:
    backend: AgentBackend
    profile: str | None = None


@dataclasses.dataclass
class RouteAgentTaskHandlerConfig(code_action.ActionHandlerConfig):
    default_backend: AgentBackend
    profiles: dict[str, AgentTaskRoute] = dataclasses.field(default_factory=dict)


_BACKEND_SOURCES: dict[AgentBackend, str] = {
    AgentBackend.PI: "fine_agent_pi.run_pi_agent_task_action.RunPiAgentTaskAction",
    AgentBackend.CLAUDE_CODE: "fine_agent_claude_code.run_claude_code_agent_task_action.RunClaudeCodeAgentTaskAction",
    AgentBackend.CODEX: "fine_agent_codex.run_codex_agent_task_action.RunCodexAgentTaskAction",
}


class RouteAgentTaskHandler(
    code_action.ActionHandler[
        RunAgentTaskAction,
        RouteAgentTaskHandlerConfig,
    ]
):
    """Route an agent task to a backend selected by role.

    The constructor owns no process, session or work lease.
    """

    def __init__(
        self,
        config: RouteAgentTaskHandlerConfig,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self._config = config
        self._runner = action_runner
        self._project_info = project_info

    async def run(
        self,
        payload: RunAgentTaskRunPayload,
        run_context: RunAgentTaskRunContext,
    ) -> RunAgentTaskRunResult:
        started = time.monotonic()
        if payload.continue_session is not None:
            failure = self._continuation_precheck(payload)
            if failure is not None:
                return failure
        if payload.profile is None:
            backend = self._config.default_backend
            backend_profile: str | None = None
        else:
            route = self._config.profiles.get(payload.profile)
            if route is None:
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=backend_support.unknown_profile_error(
                        payload.profile, self._config.profiles
                    ),
                    duration_sec=0.0,
                )
            backend = route.backend
            backend_profile = route.profile
        source = _BACKEND_SOURCES[backend]
        translated = dataclasses.replace(payload, profile=backend_profile)
        ref = iprojectactionrunner.ActionRef(
            source=source,
            result_type=RunAgentTaskRunResult,
            action_type=None,
        )
        accumulated = RunAgentTaskRunResult(
            status=AgentRunStatus.FAILED,
            error=f"backend {backend.value} returned no result",
            duration_sec=time.monotonic() - started,
        )
        received = False
        try:
            async with run_context.progress(
                f"Agent task ({backend.value})"
            ) as progress:
                async for partial in self._runner.run_action_iter(
                    ref, translated, run_context.meta, progress=progress
                ):
                    if isinstance(partial, RunAgentTaskRunResult):
                        if not received:
                            accumulated = partial
                            received = True
                        else:
                            accumulated.update(partial)
                    else:
                        accumulated = typing.cast(RunAgentTaskRunResult, partial)
                        received = True
        except (
            iprojectactionrunner.ActionRunFailed,
            iprojectactionrunner.ActionNotFound,
        ) as exc:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=f"backend {backend.value} ({source}): {exc.message}",
                duration_sec=time.monotonic() - started,
            )
        if not received:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=(f"backend {backend.value} ({source}) returned no result"),
                duration_sec=time.monotonic() - started,
            )
        return accumulated

    def _continuation_precheck(
        self, payload: RunAgentTaskRunPayload
    ) -> RunAgentTaskRunResult | None:
        """Ordered continuation checks before delegation.

        Invalid ID beats backend mismatch, which beats project mismatch,
        which beats an unknown role. An unresolved role selects no backend,
        so there is no backend comparison to invent for it.
        """
        ref = payload.continue_session
        assert ref is not None
        started = time.monotonic()
        id_error = session_id_error(ref.session_id)
        if id_error is not None:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=id_error,
                duration_sec=time.monotonic() - started,
            )
        backend: AgentBackend | None
        if payload.profile is None:
            backend = self._config.default_backend
        else:
            route = self._config.profiles.get(payload.profile)
            backend = route.backend if route is not None else None
        if backend is not None:
            backend_error = session_backend_error(ref, backend)
            if backend_error is not None:
                return RunAgentTaskRunResult(
                    status=AgentRunStatus.FAILED,
                    error=backend_error,
                    duration_sec=time.monotonic() - started,
                )
        project_error = session_project_error(
            ref, self._project_info.get_current_project_dir_path()
        )
        if project_error is not None:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=project_error,
                duration_sec=time.monotonic() - started,
            )
        if payload.profile is not None and backend is None:
            return RunAgentTaskRunResult(
                status=AgentRunStatus.FAILED,
                error=backend_support.unknown_profile_error(
                    payload.profile, self._config.profiles
                ),
                duration_sec=0.0,
            )
        return None
