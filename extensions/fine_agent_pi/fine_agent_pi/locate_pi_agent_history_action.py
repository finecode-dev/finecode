"""Backend-specific history lookup action for Pi."""

from fine_agent.locate_agent_history_action import LocateAgentHistoryAction

__all__ = ["LocatePiAgentHistoryAction"]


class LocatePiAgentHistoryAction(LocateAgentHistoryAction):
    """Locate a Pi session's history.

    Inherits the exact contract from :class:`LocateAgentHistoryAction`; the
    router references it explicitly, and ``LANGUAGE`` stays ``None`` so
    language discovery excludes it.
    """

    PARENT_ACTION = LocateAgentHistoryAction
    LANGUAGE = None
