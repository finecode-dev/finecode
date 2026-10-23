"""Unit tests for the Claude Code native-history locator."""

from __future__ import annotations

import json
import pathlib

from fine_agent.history_types import (
    HistoryDiscoveryStatus,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)

from fine_agent_claude_code.claude_code_history import (
    ClaudeObservationState,
    locate_claude_history,
)


def _provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.CLAUDE_MODELS,
        token_scope=UsageScope.SESSION,
        cost_source=UsageSource.CLAUDE_COST,
        cost_scope=UsageScope.SESSION,
    )


def _write_main(root: pathlib.Path, proj: str, session_id: str) -> pathlib.Path:
    path = root / "projects" / proj / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        json.dumps({"type": "user", "sessionId": session_id}) + "\n", encoding="utf-8"
    )
    return path


def test_locate_returns_available_for_matching_main(tmp_path: pathlib.Path) -> None:
    _write_main(tmp_path, "testproj", "sess-1")
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert len(observation.files) == 1
    assert observation.files[0].native_session_id == "sess-1"


def test_locate_is_unavailable_without_session_id(tmp_path: pathlib.Path) -> None:
    state = ClaudeObservationState(
        session_id=None, usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE
    assert observation.files == []


def test_locate_is_unavailable_when_file_missing(tmp_path: pathlib.Path) -> None:
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE


def test_locate_fails_for_duplicate_mains(tmp_path: pathlib.Path) -> None:
    _write_main(tmp_path, "proja", "sess-1")
    _write_main(tmp_path, "projb", "sess-1")
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.FAILED
    assert observation.files == []


def test_locate_fails_when_records_do_not_match(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "projects" / "testproj" / "sess-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        json.dumps({"type": "user", "sessionId": "other"}) + "\n", encoding="utf-8"
    )
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.FAILED


def test_locate_collects_related_subtree(tmp_path: pathlib.Path) -> None:
    main = _write_main(tmp_path, "testproj", "sess-1")
    related_dir = main.parent / "sess-1" / "subagents"
    related_dir.mkdir(parents=True, exist_ok=True)
    _ = (related_dir / "agent-x.jsonl").write_text(
        json.dumps({"type": "user", "agentId": "x"}) + "\n", encoding="utf-8"
    )
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert len(observation.files) == 2
    assert observation.related_status is RelatedHistoryStatus.SCANNED
