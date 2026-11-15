"""Locate a Pi session's native history by id or marker."""

from __future__ import annotations

import dataclasses

from fine_agent.history_types import UsageProvenance, UsageScope, UsageSource
from fine_agent.locate_agent_history_action import (
    LocateAgentHistoryAction,
    LocateAgentHistoryRunContext,
    LocateAgentHistoryRunPayload,
    LocateAgentHistoryRunResult,
)
from finecode_extension_api import code_action

from fine_agent_pi import pi_history
from fine_agent_pi.pi_history import PiObservationState

__all__ = [
    "LocatePiHistoryHandler",
    "LocatePiHistoryHandlerConfig",
]


@dataclasses.dataclass
class LocatePiHistoryHandlerConfig(code_action.ActionHandlerConfig): ...


class LocatePiHistoryHandler(
    code_action.ActionHandler[
        LocateAgentHistoryAction,
        LocatePiHistoryHandlerConfig,
    ]
):
    """Find a session under the Pi session directory
    (`PI_CODING_AGENT_SESSION_DIR` or `~/.pi/agent/sessions`).

    Pi names session files by time, not id, so a lookup by id reads each
    file's header. A session found here was not driven by FineCode, so no
    usage was observed: provenance is unknown.
    """

    def __init__(self, config: LocatePiHistoryHandlerConfig) -> None:
        self._config = config

    async def run(
        self,
        payload: LocateAgentHistoryRunPayload,
        run_context: LocateAgentHistoryRunContext,
    ) -> LocateAgentHistoryRunResult:
        _ = run_context
        native_root = pi_history.pi_sessions_root()
        session_id, session_file, gaps = pi_history.find_pi_session_file(
            native_root,
            payload.session_id,
            payload.marker if payload.session_id is None else None,
        )
        state = PiObservationState(
            session_id=session_id,
            session_file=str(session_file) if session_file is not None else None,
            usage_provenance=UsageProvenance(
                token_source=UsageSource.UNKNOWN,
                token_scope=UsageScope.UNKNOWN,
                cost_source=UsageSource.UNKNOWN,
                cost_scope=UsageScope.UNKNOWN,
            ),
            gaps=gaps,
        )
        return LocateAgentHistoryRunResult(
            observation=pi_history.locate_pi_history(state, native_root)
        )
