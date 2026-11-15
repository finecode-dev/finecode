"""Unit tests for the Codex native rollout locator."""

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

from fine_agent_codex.codex_history import (
    CodexObservationState,
    find_codex_thread_by_marker,
    locate_codex_history,
    read_rollout_facts,
)


def _provenance() -> UsageProvenance:
    return UsageProvenance(
        token_source=UsageSource.CODEX_TURN,
        token_scope=UsageScope.UNKNOWN,
        cost_source=UsageSource.UNKNOWN,
        cost_scope=UsageScope.UNKNOWN,
    )


def _write_rollout(
    root: pathlib.Path,
    thread_id: str,
    parent: str | None = None,
    extra: dict[str, object] | None = None,
) -> pathlib.Path:
    base = root / "sessions" / "2026" / "01" / "01"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    payload: dict[str, object] = {"id": thread_id, "cli_version": "0.163.0"}
    if parent is not None:
        payload["parent_thread_id"] = parent
    if extra:
        payload.update(extra)
    meta = {"timestamp": "t", "ordinal": 0, "type": "session_meta", "payload": payload}
    _ = path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


def test_locate_returns_available_for_matching_rollout(tmp_path: pathlib.Path) -> None:
    _write_rollout(tmp_path, "thread-1")
    state = CodexObservationState(
        thread_id="thread-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_codex_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert len(observation.files) == 1
    assert observation.backend_version == "0.163.0"


def test_locate_is_unavailable_without_thread_id(tmp_path: pathlib.Path) -> None:
    state = CodexObservationState(
        thread_id=None, usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_codex_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE


def test_locate_collects_child_and_grandchild(tmp_path: pathlib.Path) -> None:
    _write_rollout(tmp_path, "thread-1")
    _write_rollout(tmp_path, "child-1", parent="thread-1")
    _write_rollout(tmp_path, "grandchild-1", parent="child-1")
    _write_rollout(tmp_path, "other-1")
    fork_path = _write_rollout(tmp_path, "fork-1", parent="other-1")
    with fork_path.open("a", encoding="utf-8") as handle:
        _ = handle.write("")
    state = CodexObservationState(
        thread_id="thread-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_codex_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    related_ids = {
        file.native_session_id
        for file in observation.files
        if file.role.value == "related"
    }
    assert related_ids == {"child-1", "grandchild-1"}
    assert observation.related_status is RelatedHistoryStatus.SCANNED


def test_locate_fails_for_duplicate_rollouts(tmp_path: pathlib.Path) -> None:
    _write_rollout(tmp_path, "thread-1")
    archived = tmp_path / "archived_sessions" / "2026"
    archived.mkdir(parents=True, exist_ok=True)
    dup = archived / "rollout-2026-01-01T00-00-00-thread-1.jsonl"
    payload = {"id": "thread-1", "cli_version": "0.163.0"}
    meta = {"timestamp": "t", "ordinal": 0, "type": "session_meta", "payload": payload}
    _ = dup.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    state = CodexObservationState(
        thread_id="thread-1", usage_provenance=_provenance(), gaps=[]
    )

    observation = locate_codex_history(state, tmp_path)

    assert observation.history_status is HistoryDiscoveryStatus.FAILED


def _append(path: pathlib.Path, *entries: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(entry) + "\n" for entry in entries)


def _turn(model: str) -> dict[str, object]:
    return {"timestamp": "t", "type": "turn_context", "payload": {"model": model}}


def _token_count(info: object) -> dict[str, object]:
    return {
        "timestamp": "t",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def test_rollout_facts_report_provider_model_and_turns(tmp_path: pathlib.Path) -> None:
    path = _write_rollout(tmp_path, "thread-1", extra={"model_provider": "openai"})
    _append(path, _turn("gpt-x"), _token_count(None), _token_count({}), _token_count({}))

    facts = read_rollout_facts(path)

    assert facts.provider == "openai"
    assert facts.model == "gpt-x"
    assert facts.turns == 2


def test_rollout_facts_leave_unreported_and_mixed_models_unknown(
    tmp_path: pathlib.Path,
) -> None:
    path = _write_rollout(tmp_path, "thread-1")
    _append(path, _turn("gpt-x"), _turn("gpt-y"))

    facts = read_rollout_facts(path)

    assert facts.provider is None
    assert facts.model is None
    assert facts.turns is None
    assert read_rollout_facts(tmp_path / "missing.jsonl").model is None


def _event(text: str) -> dict[str, object]:
    return {"type": "event", "text": text}


def test_marker_finds_the_root_thread_that_printed_it(tmp_path: pathlib.Path) -> None:
    """A spawned thread can carry its parent's history, so the marker appears
    in both; the root thread is the session that ran the command."""
    _append(_write_rollout(tmp_path, "thread-1"), _event("recorded fc-m-1"))
    _append(_write_rollout(tmp_path, "thread-2", parent="thread-1"), _event("fc-m-1"))
    _ = _write_rollout(tmp_path, "thread-3")

    assert find_codex_thread_by_marker(tmp_path, "fc-m-1") == ("thread-1", [])


def test_marker_in_two_root_threads_identifies_neither(tmp_path: pathlib.Path) -> None:
    _append(_write_rollout(tmp_path, "thread-1"), _event("fc-m-1"))
    _append(_write_rollout(tmp_path, "thread-2"), _event("fc-m-1"))

    thread_id, gaps = find_codex_thread_by_marker(tmp_path, "fc-m-1")

    assert thread_id is None
    assert gaps == ["multiple codex threads contain marker fc-m-1: thread-1, thread-2"]


def test_missing_marker_is_a_gap(tmp_path: pathlib.Path) -> None:
    _ = _write_rollout(tmp_path, "thread-1")

    assert find_codex_thread_by_marker(tmp_path, "fc-m-1") == (
        None,
        ["no codex rollout contains marker fc-m-1"],
    )
