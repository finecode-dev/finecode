"""Unit tests for the Claude Code native-history locator."""

from __future__ import annotations

import json
import pathlib

from fine_agent.history_types import (
    HistoryDiscoveryStatus,
    NativeHistoryFormat,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)

from fine_agent_claude_code.claude_code_history import (
    ClaudeObservationState,
    find_claude_session_by_marker,
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


def test_sidecar_files_beside_the_session_are_related_history(
    tmp_path: pathlib.Path,
) -> None:
    """Claude Code stores large tool results and subagent metadata as plain
    files beside the session; without them the transcript points at nothing."""
    main = _write_main(tmp_path, "testproj", "sess-1")
    sibling = main.parent / "sess-1"
    (sibling / "tool-results").mkdir(parents=True)
    _ = (sibling / "tool-results" / "b1.txt").write_text("big output", "utf-8")
    (sibling / "subagents").mkdir()
    _ = (sibling / "subagents" / "agent-a.jsonl").write_text(
        json.dumps({"agentId": "a", "sessionId": "sess-1"}) + "\n", "utf-8"
    )
    _ = (sibling / "subagents" / "agent-a.meta.json").write_text("{}", "utf-8")
    state = ClaudeObservationState(
        session_id="sess-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_claude_history(state, tmp_path)

    formats = {file.native_relative_path: file.format for file in observation.files}
    assert formats == {
        "projects/testproj/sess-1.jsonl": NativeHistoryFormat.CLAUDE_CODE,
        "projects/testproj/sess-1/subagents/agent-a.jsonl": (
            NativeHistoryFormat.CLAUDE_CODE
        ),
        "projects/testproj/sess-1/subagents/agent-a.meta.json": (
            NativeHistoryFormat.CLAUDE_SIDECAR
        ),
        "projects/testproj/sess-1/tool-results/b1.txt": (
            NativeHistoryFormat.CLAUDE_SIDECAR
        ),
    }
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE


def test_marker_finds_the_one_session_that_printed_it(tmp_path: pathlib.Path) -> None:
    main = _write_main(tmp_path, "proj", "sess-1")
    with main.open("a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps({"content": "recorded fc-m-1"}) + "\n")
    _ = _write_main(tmp_path, "proj", "sess-2")

    assert find_claude_session_by_marker(tmp_path, "fc-m-1") == ("sess-1", [])

    session_id, gaps = find_claude_session_by_marker(tmp_path, "fc-m-9")
    assert session_id is None
    assert gaps == ["no claude session contains marker fc-m-9"]


def test_marker_in_two_sessions_identifies_neither(tmp_path: pathlib.Path) -> None:
    for session_id in ("sess-1", "sess-2"):
        main = _write_main(tmp_path, "proj", session_id)
        with main.open("a", encoding="utf-8") as handle:
            _ = handle.write("fc-m-1\n")

    session_id, gaps = find_claude_session_by_marker(tmp_path, "fc-m-1")

    assert session_id is None
    assert "multiple claude sessions contain marker fc-m-1" in gaps[0]
