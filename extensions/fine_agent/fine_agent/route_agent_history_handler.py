"""Backend router for locating agent session history."""

from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner

from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.locate_agent_history_action import (
    LocateAgentHistoryAction,
    LocateAgentHistoryRunContext,
    LocateAgentHistoryRunPayload,
    LocateAgentHistoryRunResult,
)

__all__ = [
    "RouteAgentHistoryHandler",
    "RouteAgentHistoryHandlerConfig",
]


@dataclasses.dataclass
class RouteAgentHistoryHandlerConfig(code_action.ActionHandlerConfig): ...


_BACKEND_SOURCES: dict[AgentBackend, str] = {
    AgentBackend.PI: "fine_agent_pi.locate_pi_agent_history_action.LocatePiAgentHistoryAction",
    AgentBackend.CLAUDE_CODE: "fine_agent_claude_code.locate_claude_code_agent_history_action.LocateClaudeCodeAgentHistoryAction",
    AgentBackend.CODEX: "fine_agent_codex.locate_codex_agent_history_action.LocateCodexAgentHistoryAction",
}


def _failed_observation(
    backend: AgentBackend, session_id: str | None, gap: str
) -> AgentRunObservation:
    return AgentRunObservation(
        backend=backend,
        backend_version=None,
        session_id=session_id,
        history_root=None,
        history_status=HistoryDiscoveryStatus.FAILED,
        files=[],
        related_status=RelatedHistoryStatus.UNAVAILABLE,
        usage_provenance=UsageProvenance(
            token_source=UsageSource.UNKNOWN,
            token_scope=UsageScope.UNKNOWN,
            cost_source=UsageSource.UNKNOWN,
            cost_scope=UsageScope.UNKNOWN,
        ),
        gaps=[gap],
    )


class RouteAgentHistoryHandler(
    code_action.ActionHandler[
        LocateAgentHistoryAction,
        RouteAgentHistoryHandlerConfig,
    ]
):
    """Dispatch a history lookup to the leaf action of the payload's backend.
    """

    def __init__(
        self,
        config: RouteAgentHistoryHandlerConfig,
        action_runner: iprojectactionrunner.IProjectActionRunner,
    ) -> None:
        self._config = config
        self._runner = action_runner

    async def run(
        self,
        payload: LocateAgentHistoryRunPayload,
        run_context: LocateAgentHistoryRunContext,
    ) -> LocateAgentHistoryRunResult:
        source = _BACKEND_SOURCES[payload.backend]
        ref = iprojectactionrunner.ActionRef(
            source=source,
            result_type=LocateAgentHistoryRunResult,
            action_type=None,
        )
        try:
            result = await self._runner.run_action(ref, payload, run_context.meta)
        except (
            iprojectactionrunner.ActionRunFailed,
            iprojectactionrunner.ActionNotFound,
        ) as exc:
            return LocateAgentHistoryRunResult(
                observation=_failed_observation(
                    payload.backend,
                    payload.session_id,
                    f"backend {payload.backend.value} ({source}): {exc.message}",
                )
            )
        if not isinstance(result, LocateAgentHistoryRunResult) or (
            result.observation is None
        ):
            return LocateAgentHistoryRunResult(
                observation=_failed_observation(
                    payload.backend,
                    payload.session_id,
                    f"backend {payload.backend.value} ({source}) returned no result",
                )
            )
        return result
