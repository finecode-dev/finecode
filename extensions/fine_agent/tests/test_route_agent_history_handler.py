"""History router unit tests with a recording runner double."""

from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner.testing import run_handler

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
    LocateAgentHistoryRunPayload,
    LocateAgentHistoryRunResult,
)
from fine_agent.route_agent_history_handler import RouteAgentHistoryHandler

_CANONICAL = {
    AgentBackend.PI: (
        "fine_agent_pi.locate_pi_agent_history_action.LocatePiAgentHistoryAction"
    ),
    AgentBackend.CLAUDE_CODE: (
        "fine_agent_claude_code.locate_claude_code_agent_history_action."
        "LocateClaudeCodeAgentHistoryAction"
    ),
    AgentBackend.CODEX: (
        "fine_agent_codex.locate_codex_agent_history_action."
        "LocateCodexAgentHistoryAction"
    ),
}


def _observation(backend: AgentBackend) -> AgentRunObservation:
    return AgentRunObservation(
        backend=backend,
        backend_version=None,
        session_id="s-1",
        history_root=None,
        history_status=HistoryDiscoveryStatus.AVAILABLE,
        files=[],
        related_status=RelatedHistoryStatus.SCANNED,
        usage_provenance=UsageProvenance(
            token_source=UsageSource.UNKNOWN,
            token_scope=UsageScope.UNKNOWN,
            cost_source=UsageSource.UNKNOWN,
            cost_scope=UsageScope.UNKNOWN,
        ),
        gaps=[],
    )


@dataclasses.dataclass
class _Runner:
    raises: Exception | None = None
    calls: list[tuple[str, LocateAgentHistoryRunPayload]] = dataclasses.field(
        default_factory=list
    )

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: LocateAgentHistoryRunPayload,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> LocateAgentHistoryRunResult:
        _ = (meta, caller_kwargs)
        self.calls.append((action_type.source, payload))
        if self.raises is not None:
            raise self.raises
        return LocateAgentHistoryRunResult(observation=_observation(payload.backend))


async def _route(
    runner: _Runner, payload: LocateAgentHistoryRunPayload
) -> LocateAgentHistoryRunResult:
    result = await run_handler(
        RouteAgentHistoryHandler,
        payload,
        action_cls=LocateAgentHistoryAction,
        service_overrides={iprojectactionrunner.IProjectActionRunner: runner},
    )
    assert isinstance(result, LocateAgentHistoryRunResult)
    return result


async def test_each_backend_routes_to_its_canonical_leaf() -> None:
    for backend, source in _CANONICAL.items():
        runner = _Runner()
        payload = LocateAgentHistoryRunPayload(backend=backend, marker="m")

        result = await _route(runner, payload)

        assert runner.calls == [(source, payload)]
        assert result.observation is not None
        assert result.observation.backend is backend


async def test_unregistered_leaf_is_a_failed_observation_naming_it() -> None:
    """A backend whose leaf action is not configured answers with a failed
    observation, so a collector reports the session instead of crashing."""
    runner = _Runner(raises=iprojectactionrunner.ActionNotFound("not configured"))

    result = await _route(
        runner,
        LocateAgentHistoryRunPayload(backend=AgentBackend.CODEX, session_id="t-1"),
    )

    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.FAILED
    assert result.observation.session_id == "t-1"
    assert _CANONICAL[AgentBackend.CODEX] in result.observation.gaps[0]
    assert result.return_code is code_action.RunReturnCode.ERROR
