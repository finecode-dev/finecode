"""Backend-specific agent action for pi."""

from fine_agent.run_agent_task_action import RunAgentTaskAction

__all__ = ["RunPiAgentTaskAction"]


class RunPiAgentTaskAction(RunAgentTaskAction):
    """Run an agent task with the pi backend.

    Inherits the exact payload, context, result, project scope and sequential
    execution from :class:`RunAgentTaskAction`. Sets ``PARENT_ACTION`` so the
    router can reference it explicitly; ``LANGUAGE`` stays ``None`` so
    language discovery excludes it.
    """

    PARENT_ACTION = RunAgentTaskAction
    LANGUAGE = None
