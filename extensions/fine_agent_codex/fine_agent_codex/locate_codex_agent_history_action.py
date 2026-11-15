"""Backend-specific history lookup action for Codex."""

from fine_agent.locate_agent_history_action import LocateAgentHistoryAction

__all__ = ["LocateCodexAgentHistoryAction"]


class LocateCodexAgentHistoryAction(LocateAgentHistoryAction):
    """Locate a Codex thread's rollout history.

    Inherits the exact contract from :class:`LocateAgentHistoryAction`; the
    router references it explicitly, and ``LANGUAGE`` stays ``None`` so
    language discovery excludes it.
    """

    PARENT_ACTION = LocateAgentHistoryAction
    LANGUAGE = None
