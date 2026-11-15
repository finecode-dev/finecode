"""Unit tests for the Pi native-history locator."""

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

from fine_agent_pi.pi_history import (
    PiObservationState,
    find_pi_session_file,
    locate_pi_history,
)


def _provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.PI_STATS,
        token_scope=UsageScope.SESSION,
        cost_source=UsageSource.PI_STATS,
        cost_scope=UsageScope.SESSION,
    )


def _write_session(path: pathlib.Path, session_id: str) -> None:
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": "/tmp",
    }
    _ = path.write_text(json.dumps(header) + "\n", encoding="utf-8")


def test_locate_returns_available_for_matching_header(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "2026-01-01T00-00-00_sess-1.jsonl"
    _write_session(target, "sess-1")
    state = PiObservationState(
        session_id="sess-1",
        session_file=str(target),
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert len(observation.files) == 1
    assert observation.files[0].native_session_id == "sess-1"
    assert observation.related_status is RelatedHistoryStatus.UNAVAILABLE
    assert any("child" in gap for gap in observation.gaps)


def test_locate_is_unavailable_without_stats_identity(tmp_path: pathlib.Path) -> None:
    state = PiObservationState(
        session_id=None,
        session_file=None,
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE
    assert observation.files == []
    assert observation.history_root is None


def test_locate_is_unavailable_for_missing_file(tmp_path: pathlib.Path) -> None:
    state = PiObservationState(
        session_id="sess-1",
        session_file=str(tmp_path / "missing.jsonl"),
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE
    assert observation.files == []


def test_locate_fails_for_header_mismatch(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "2026-01-01T00-00-00_sess-1.jsonl"
    _write_session(target, "other-id")
    state = PiObservationState(
        session_id="sess-1",
        session_file=str(target),
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.FAILED
    assert observation.files == []
    assert any("header" in gap for gap in observation.gaps)


def test_locate_fails_for_duplicate_matching_sessions(tmp_path: pathlib.Path) -> None:
    first = tmp_path / "2026-01-01T00-00-00_sess-1.jsonl"
    second = tmp_path / "2026-01-02T00-00-00_sess-1.jsonl"
    _write_session(first, "sess-1")
    _write_session(second, "sess-1")
    state = PiObservationState(
        session_id="sess-1",
        session_file=str(first),
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.FAILED
    assert observation.files == []
    assert any("multiple" in gap for gap in observation.gaps)


def test_locate_ignores_wrong_session_decoy(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "2026-01-01T00-00-00_sess-1.jsonl"
    decoy = tmp_path / "2026-01-02T00-00-00_wrong.jsonl"
    _write_session(target, "sess-1")
    _write_session(decoy, "wrong-id")
    state = PiObservationState(
        session_id="sess-1",
        session_file=str(target),
        usage_provenance=_provenance(),
        gaps=[],
    )

    observation = locate_pi_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert len(observation.files) == 1
    assert observation.files[0].native_session_id == "sess-1"


def test_session_file_is_found_by_header_id(tmp_path: pathlib.Path) -> None:
    """Pi names files by time, so a session known only by id is found by
    reading headers, in any project subdirectory."""
    project = tmp_path / "--workspaces-proj--"
    project.mkdir()
    target = project / "2026-01-01T00-00-00_x.jsonl"
    _write_session(target, "sess-1")
    _write_session(project / "2026-01-02T00-00-00_y.jsonl", "sess-2")

    assert find_pi_session_file(tmp_path, "sess-1", None) == ("sess-1", target, [])


def test_session_file_is_found_by_marker(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "a.jsonl"
    _write_session(target, "sess-1")
    with target.open("a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps({"type": "message", "text": "fc-m-1"}) + "\n")
    _write_session(tmp_path / "b.jsonl", "sess-2")

    assert find_pi_session_file(tmp_path, None, "fc-m-1") == ("sess-1", target, [])

    assert find_pi_session_file(tmp_path, None, "fc-m-9") == (
        None,
        None,
        ["no pi session file matches marker fc-m-9"],
    )
