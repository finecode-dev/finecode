"""Locate a Claude Code session's native history by id or marker."""

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

from fine_agent_claude_code import claude_code_history
from fine_agent_claude_code.claude_code_history import ClaudeObservationState

__all__ = [
    "LocateClaudeCodeHistoryHandler",
    "LocateClaudeCodeHistoryHandlerConfig",
]


@dataclasses.dataclass
class LocateClaudeCodeHistoryHandlerConfig(code_action.ActionHandlerConfig): ...


class LocateClaudeCodeHistoryHandler(
    code_action.ActionHandler[
        LocateAgentHistoryAction,
        LocateClaudeCodeHistoryHandlerConfig,
    ]
):
    """Find a session under the Claude config root (`CLAUDE_CONFIG_DIR` or `~/.claude`).

    A session found here was not driven by FineCode, so no usage was observed:
    provenance is unknown.
    """

    def __init__(self, config: LocateClaudeCodeHistoryHandlerConfig) -> None:
        self._config = config

    async def run(
        self,
        payload: LocateAgentHistoryRunPayload,
        run_context: LocateAgentHistoryRunContext,
    ) -> LocateAgentHistoryRunResult:
        _ = run_context
        native_root = claude_code_history.claude_config_root()
        session_id = payload.session_id
        gaps: list[str] = []
        if session_id is None and payload.marker is not None:
            session_id, gaps = claude_code_history.find_claude_session_by_marker(
                native_root, payload.marker
            )
        state = ClaudeObservationState(
            session_id=session_id,
            usage_provenance=UsageProvenance(
                token_source=UsageSource.UNKNOWN,
                token_scope=UsageScope.UNKNOWN,
                cost_source=UsageSource.UNKNOWN,
                cost_scope=UsageScope.UNKNOWN,
            ),
            gaps=gaps,
        )
        return LocateAgentHistoryRunResult(
            observation=claude_code_history.locate_claude_history(state, native_root)
        )
