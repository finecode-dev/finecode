"""Locate one agent session's native history, wherever the session was started."""

from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action

from fine_agent.history_types import AgentBackend, AgentRunObservation

__all__ = [
    "LocateAgentHistoryAction",
    "LocateAgentHistoryRunContext",
    "LocateAgentHistoryRunPayload",
    "LocateAgentHistoryRunResult",
]


@dataclasses.dataclass
class LocateAgentHistoryRunPayload(code_action.RunActionPayload):
    backend: AgentBackend
    """The agent that produced the session. It decides where its history lives."""
    session_id: str | None = None
    """The backend's own session (Codex: thread) id, or `None` to find the
    session by `marker`."""
    marker: str | None = None
    """A string the session's main history is known to contain, used to find
    the session when `session_id` is `None`. Ignored when `session_id` is set."""


class LocateAgentHistoryRunContext(
    code_action.RunActionContext[LocateAgentHistoryRunPayload]
): ...


@dataclasses.dataclass
class LocateAgentHistoryRunResult(code_action.RunActionResult):
    observation: AgentRunObservation | None = None
    """What was found. `None` only when no handler answered; a session that
    could not be found is an observation with `UNAVAILABLE` status and gaps."""

    def update(self, other: code_action.RunActionResult) -> None:
        # One handler (see `LocateAgentHistoryAction`), so last-writer-wins.
        if not isinstance(other, LocateAgentHistoryRunResult):
            return
        self.observation = other.observation

    def to_text(self) -> str:
        if self.observation is None:
            return "no history observation"
        observation = self.observation
        lines = [
            (
                f"{observation.backend.value} session "
                f"{observation.session_id or '?'}: {observation.history_status.value}"
            )
        ]
        lines.extend(
            f"{file.role.value}: {file.native_relative_path}"
            for file in observation.files
        )
        lines.extend(f"gap: {gap}" for gap in observation.gaps)
        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.observation is None or not self.observation.files:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class LocateAgentHistoryAction(
    code_action.Action[
        LocateAgentHistoryRunPayload,
        LocateAgentHistoryRunContext,
        LocateAgentHistoryRunResult,
    ]
):
    """Find the native history files of one agent session, without copying them.

    The `run_agent_task` handlers observe the session they drive themselves
    (`capture_history`). This action covers a session nobody drove: one a
    person started in the agent's own UI, identified afterwards by its id or
    by a marker string it wrote.

    Handler contract:
    - Never copy or modify a file; report paths only.
    - A session that cannot be found is an observation with `UNAVAILABLE`
      status and a gap saying why, not an exception.
    - Discovery by `marker` searches main session files only, and more than one
      match is `FAILED` with no files: two sessions cannot both have written
      one marker.
    """

    DESCRIPTION = (
        "Find the native history files of one AI agent session by its id or a "
        "marker it wrote."
    )
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
    PAYLOAD_TYPE = LocateAgentHistoryRunPayload
    RUN_CONTEXT_TYPE = LocateAgentHistoryRunContext
    RESULT_TYPE = LocateAgentHistoryRunResult
