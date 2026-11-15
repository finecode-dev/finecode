"""Backend-specific agent action for Claude Code."""

from fine_agent.run_agent_task_action import RunAgentTaskAction

__all__ = ["RunClaudeCodeAgentTaskAction"]


class RunClaudeCodeAgentTaskAction(RunAgentTaskAction):
    """Run an agent task with the Claude Code backend.

    Inherits the exact payload, context, result, project scope and sequential
    execution from :class:`RunAgentTaskAction`. Sets ``PARENT_ACTION`` so the
    router can reference it explicitly; ``LANGUAGE`` stays ``None`` so
    language discovery excludes it.
    """

    PARENT_ACTION = RunAgentTaskAction
    LANGUAGE = None
