"""Session references survive the WM converter."""

from __future__ import annotations

import pathlib

from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner._converter import converter

from fine_agent.history_types import AgentBackend
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentSessionRef,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)


def test_session_payload_and_result_round_trip_through_converter(
    tmp_path: pathlib.Path,
) -> None:
    """Session references survive the WM converter for every backend."""
    project_uri = path_to_resource_uri(tmp_path)
    for backend in (
        AgentBackend.PI,
        AgentBackend.CLAUDE_CODE,
        AgentBackend.CODEX,
    ):
        ref = AgentSessionRef(
            backend=backend,
            session_id="33333333-3333-4333-8333-333333333333",
            project_dir=project_uri,
        )
        payload = RunAgentTaskRunPayload(
            prompt="hello", profile=None, continue_session=ref
        )
        revived_payload = converter.structure(
            converter.unstructure(payload), RunAgentTaskRunPayload
        )
        assert revived_payload.continue_session == ref
        result = RunAgentTaskRunResult(
            status=AgentRunStatus.SETTLED, output="hi", session=ref
        )
        revived = converter.structure(
            converter.unstructure(result), RunAgentTaskRunResult
        )
        assert revived.session == ref
    assert (
        converter.structure(
            converter.unstructure(RunAgentTaskRunPayload(prompt="x")),
            RunAgentTaskRunPayload,
        ).continue_session
        is None
    )
    legacy = RunAgentTaskRunPayload("positional prompt")
    assert legacy.prompt == "positional prompt"
    assert legacy.continue_session is None
