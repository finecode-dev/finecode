"""Locate a Codex thread's native rollout history by id or marker."""

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

from fine_agent_codex import codex_history
from fine_agent_codex.codex_history import CodexObservationState

__all__ = [
    "LocateCodexHistoryHandler",
    "LocateCodexHistoryHandlerConfig",
]


@dataclasses.dataclass
class LocateCodexHistoryHandlerConfig(code_action.ActionHandlerConfig): ...


class LocateCodexHistoryHandler(
    code_action.ActionHandler[
        LocateAgentHistoryAction,
        LocateCodexHistoryHandlerConfig,
    ]
):
    """Find a thread under the Codex home (`CODEX_HOME` or `~/.codex`).

    `session_id` is the Codex thread id. A thread found here was not driven by
    FineCode, so no usage was observed: provenance is unknown.
    """

    def __init__(self, config: LocateCodexHistoryHandlerConfig) -> None:
        self._config = config

    async def run(
        self,
        payload: LocateAgentHistoryRunPayload,
        run_context: LocateAgentHistoryRunContext,
    ) -> LocateAgentHistoryRunResult:
        _ = run_context
        native_root = codex_history.codex_home()
        thread_id = payload.session_id
        gaps: list[str] = []
        if thread_id is None and payload.marker is not None:
            thread_id, gaps = codex_history.find_codex_thread_by_marker(
                native_root, payload.marker
            )
        state = CodexObservationState(
            thread_id=thread_id,
            usage_provenance=UsageProvenance(
                token_source=UsageSource.UNKNOWN,
                token_scope=UsageScope.UNKNOWN,
                cost_source=UsageSource.UNKNOWN,
                cost_scope=UsageScope.UNKNOWN,
            ),
            gaps=gaps,
        )
        return LocateAgentHistoryRunResult(
            observation=codex_history.locate_codex_history(state, native_root)
        )
