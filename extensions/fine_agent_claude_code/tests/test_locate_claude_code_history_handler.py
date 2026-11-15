"""`LocateClaudeCodeHistoryHandler` against a fake Claude config directory."""

from __future__ import annotations

import json
import pathlib

import pytest
from fine_agent.history_types import (
    AgentBackend,
    HistoryDiscoveryStatus,
    UsageSource,
)
from fine_agent.locate_agent_history_action import (
    LocateAgentHistoryRunPayload,
    LocateAgentHistoryRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_runner.testing import run_handler

from fine_agent_claude_code import (
    LocateClaudeCodeAgentHistoryAction,
    LocateClaudeCodeHistoryHandler,
)


def _session(root: pathlib.Path, session_id: str, *lines: str) -> None:
    path = root / "projects" / "proj" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [json.dumps({"type": "user", "sessionId": session_id}), *lines]
    _ = path.write_text("\n".join(records) + "\n", encoding="utf-8")


async def _locate(payload: LocateAgentHistoryRunPayload) -> LocateAgentHistoryRunResult:
    result = await run_handler(
        LocateClaudeCodeHistoryHandler,
        payload,
        action_cls=LocateClaudeCodeAgentHistoryAction,
    )
    assert isinstance(result, LocateAgentHistoryRunResult)
    return result


async def test_session_without_id_is_found_by_its_marker(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _session(tmp_path, "sess-1", json.dumps({"content": "recorded fc-m-1"}))
    _session(tmp_path, "sess-2")

    result = await _locate(
        LocateAgentHistoryRunPayload(backend=AgentBackend.CLAUDE_CODE, marker="fc-m-1")
    )

    assert result.observation is not None
    assert result.observation.session_id == "sess-1"
    assert result.observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    # Nobody drove this session, so no usage was observed.
    assert result.observation.usage_provenance.token_source is UsageSource.UNKNOWN


async def test_given_id_is_used_as_is(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _session(tmp_path, "sess-2")

    result = await _locate(
        LocateAgentHistoryRunPayload(
            backend=AgentBackend.CLAUDE_CODE, session_id="sess-2", marker="unused"
        )
    )

    assert result.observation is not None
    assert result.observation.session_id == "sess-2"
    assert result.return_code is code_action.RunReturnCode.SUCCESS
