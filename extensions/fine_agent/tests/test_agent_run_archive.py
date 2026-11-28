"""Tests for archiving one agent run's request and native history.

The archive is the durable record of what an agent was asked and what it did;
a record that silently misses a file, or overwrites another run's, reads as
complete when it is not.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_agent.agent_run_archive import (
    FileSaveStatus,
    archive_agent_run,
    archive_observed_history,
    write_archive_file,
)
from fine_agent.history_types import (
    AgentBackend,
    AgentRunObservation,
    HistoryDiscoveryStatus,
    NativeHistoryFile,
    NativeHistoryFormat,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageProvenance,
    UsageScope,
    UsageSource,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentRunUsage,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)


def _native(
    path: pathlib.Path, role: NativeHistoryRole, session_id: str | None
) -> NativeHistoryFile:
    return NativeHistoryFile(
        role=role,
        source_uri=path_to_resource_uri(path),
        native_relative_path=path.name,
        native_session_id=session_id,
        format=NativeHistoryFormat.PI,
    )


def _result(
    root: pathlib.Path, files: list[NativeHistoryFile]
) -> RunAgentTaskRunResult:
    return RunAgentTaskRunResult(
        status=AgentRunStatus.SETTLED,
        output="answer",
        turns=3,
        usage=AgentRunUsage(total_tokens=10, provider="p", model="m"),
        duration_sec=1.5,
        observation=AgentRunObservation(
            backend=AgentBackend.PI,
            backend_version="1.0",
            session_id="sess-1",
            history_root=path_to_resource_uri(root),
            history_status=HistoryDiscoveryStatus.AVAILABLE,
            files=files,
            related_status=RelatedHistoryStatus.SCANNED,
            usage_provenance=UsageProvenance(
                token_source=UsageSource.PI_STATS,
                token_scope=UsageScope.SESSION,
                cost_source=UsageSource.UNKNOWN,
                cost_scope=UsageScope.UNKNOWN,
            ),
            gaps=[],
        ),
    )


def _dirs(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    native = tmp_path / "native"
    native.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return native, run_dir


def test_complete_run_copies_request_and_history(tmp_path: pathlib.Path) -> None:
    native, run_dir = _dirs(tmp_path)
    main = native / "main.jsonl"
    _ = main.write_text('{"type": "session"}\n', encoding="utf-8")
    related = native / "child.jsonl"
    _ = related.write_text('{"type": "x"}\n', encoding="utf-8")
    request = RunAgentTaskRunPayload(prompt="do it", profile="role")

    archive = archive_agent_run(
        run_dir,
        request,
        _result(
            native,
            [
                _native(main, NativeHistoryRole.MAIN, "sess-1"),
                _native(related, NativeHistoryRole.RELATED, None),
            ],
        ),
    )

    assert archive["errors"] == []
    persisted = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    assert persisted["prompt"] == "do it"
    assert persisted["profile"] == "role"
    copied = run_dir / "history" / "main.jsonl"
    assert copied.read_bytes() == main.read_bytes()
    assert (run_dir / "history" / "related" / "child.jsonl").is_file()
    by_path = {entry["relative_path"]: entry for entry in archive["history"]["files"]}
    assert (
        by_path["history/main.jsonl"]["sha256"]
        == hashlib.sha256(main.read_bytes()).hexdigest()
    )
    assert by_path["history/main.jsonl"]["native_session_id"] == "sess-1"
    assert archive["agent"]["backend"] is AgentBackend.PI
    assert archive["agent"]["model"] == "m"
    assert archive["agent"]["turns"] == 3
    assert archive["usage"] is not None
    assert archive["usage"]["total_tokens"] == 10
    assert archive["duration_sec"] == 1.5


def test_undispatched_run_still_writes_a_request_record(
    tmp_path: pathlib.Path,
) -> None:
    """A step that never reached its agent still says so in its record."""
    _, run_dir = _dirs(tmp_path)

    archive = archive_agent_run(run_dir, None, None)

    assert archive["request"]["status"] is FileSaveStatus.SAVED
    assert archive["history"]["gaps"] == ["agent not dispatched"]
    assert archive["errors"] == ["agent not dispatched"]
    assert archive["status"] is None


def test_dispatched_run_without_result_is_a_gap(tmp_path: pathlib.Path) -> None:
    _, run_dir = _dirs(tmp_path)

    archive = archive_agent_run(run_dir, RunAgentTaskRunPayload(prompt="p"), None)

    assert archive["history"]["gaps"] == ["agent dispatched but returned no result"]


def test_missing_main_history_is_reported_unavailable(
    tmp_path: pathlib.Path,
) -> None:
    native, run_dir = _dirs(tmp_path)

    archive = archive_agent_run(
        run_dir, RunAgentTaskRunPayload(prompt="p"), _result(native, [])
    )

    mains = [
        entry
        for entry in archive["history"]["files"]
        if entry["relative_path"] == "history/main.jsonl"
    ]
    assert [entry["status"] for entry in mains] == [FileSaveStatus.UNAVAILABLE]
    assert archive["errors"]


def test_history_outside_the_native_root_is_not_copied(
    tmp_path: pathlib.Path,
) -> None:
    """The observation names the files; the archive refuses ones it does not own."""
    native, run_dir = _dirs(tmp_path)
    stray = tmp_path / "elsewhere.jsonl"
    _ = stray.write_text("secret\n", encoding="utf-8")

    archive = archive_agent_run(
        run_dir,
        RunAgentTaskRunPayload(prompt="p"),
        _result(native, [_native(stray, NativeHistoryRole.MAIN, None)]),
    )

    entry = archive["history"]["files"][0]
    assert entry["status"] is FileSaveStatus.FAILED
    assert entry["error"] is not None
    assert "outside the native history root" in entry["error"]
    assert not (run_dir / "history" / "main.jsonl").exists()


def test_write_archive_file_never_overwrites(tmp_path: pathlib.Path) -> None:
    first = write_archive_file(tmp_path, "a.json", b"one")
    second = write_archive_file(tmp_path, "a.json", b"two")

    assert first["status"] is FileSaveStatus.SAVED
    assert second["status"] is FileSaveStatus.FAILED
    assert (tmp_path / "a.json").read_bytes() == b"one"


def test_observed_history_is_archived_without_a_request(
    tmp_path: pathlib.Path,
) -> None:
    """A session nobody dispatched has an observation but no request; its
    history lands in the same layout a dispatched run's does."""
    native, run_dir = _dirs(tmp_path)
    main = native / "main.jsonl"
    _ = main.write_text('{"type": "session"}\n', encoding="utf-8")
    observation = _result(native, [_native(main, NativeHistoryRole.MAIN, "sess-1")])
    assert observation.observation is not None

    history = archive_observed_history(run_dir, observation.observation)

    assert history["discovery_status"] is HistoryDiscoveryStatus.AVAILABLE
    assert [entry["status"] for entry in history["files"]] == [FileSaveStatus.SAVED]
    assert (run_dir / "history" / "main.jsonl").read_bytes() == main.read_bytes()
    assert not (run_dir / "request.json").exists()
