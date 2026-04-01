from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import hashlib
import json
import os
import pathlib
import sys
import threading
import typing
import uuid

DEFAULT_MAX_SEGMENT_BYTES = 1_048_576  # 1 MiB
DEFAULT_MAX_SEGMENTS = 20


class WalEventType(enum.StrEnum):
    """WM WAL event types.

    Keep the event catalog centralized here so call sites do not invent ad-hoc
    string literals and replay logic has one authoritative list to follow.
    """

    RUN_ACCEPTED = "run.accepted"
    RUN_REJECTED = "run.rejected"
    RUNNER_SELECTED = "runner.selected"
    RUN_DISPATCHED = "run.dispatched"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


@dataclasses.dataclass(frozen=True)
class RunAcceptedPayload:
    """Fields written when WM accepts a run request."""

    params_hash: str


@dataclasses.dataclass(frozen=True)
class RunRejectedPayload:
    """Fields written when WM rejects a run before dispatch."""

    reason: str


@dataclasses.dataclass(frozen=True)
class RunnerSelectedPayload:
    """Fields written when WM chooses the environment for a run."""

    env_name: str


@dataclasses.dataclass(frozen=True)
class RunDispatchedPayload:
    """Fields written once the request is sent to a concrete runner."""

    runner_id: str
    env_name: str


@dataclasses.dataclass(frozen=True)
class RunCompletedPayload:
    """Fields written for a successful runner response."""

    return_code: int


@dataclasses.dataclass(frozen=True)
class RunFailedPayload:
    """Fields written when runner communication or execution fails."""

    error: str
    env_name: str


WalPayload = (
    RunAcceptedPayload
    | RunRejectedPayload
    | RunnerSelectedPayload
    | RunDispatchedPayload
    | RunCompletedPayload
    | RunFailedPayload
    | dict[str, typing.Any]
)


def new_wal_run_id() -> str:
    return str(uuid.uuid4())


def params_hash(params: dict[str, typing.Any]) -> str:
    try:
        encoded = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    except TypeError:
        encoded = str(params).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def emit_run_event(
    wal_writer: WalWriter | None,
    *,
    event_type: WalEventType,
    wal_run_id: str,
    action_name: str,
    project_path: pathlib.Path | str,
    run_trigger: str,
    dev_env: str,
    payload: WalPayload | None = None,
) -> None:
    if wal_writer is None:
        return
    wal_writer.append(
        event_type=event_type,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=str(project_path),
        trigger=run_trigger,
        dev_env=dev_env,
        payload=payload,
    )


def default_wal_dir_path() -> pathlib.Path:
    venv_dir_path = pathlib.Path(sys.executable).parent.parent
    return venv_dir_path / "state" / "finecode" / "wal" / "wm"


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _serialize_payload(payload: WalPayload | None) -> dict[str, typing.Any]:
    if payload is None:
        return {}
    if dataclasses.is_dataclass(payload):
        return dataclasses.asdict(payload)
    return payload


@dataclasses.dataclass
class WalConfig:
    enabled: bool = False
    dir_path: pathlib.Path | None = None
    max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES
    max_segments: int = DEFAULT_MAX_SEGMENTS


class WalWriter:
    """Append-only JSONL WAL with segment rotation.

    This writer is process-local and single-writer. It is designed for WM-side
    run lifecycle events and intentionally keeps records compact.
    """

    def __init__(self, config: WalConfig) -> None:
        if config.max_segment_bytes <= 0:
            raise ValueError("WalConfig.max_segment_bytes must be > 0")
        if config.max_segments <= 0:
            raise ValueError("WalConfig.max_segments must be > 0")

        self._dir_path = config.dir_path or default_wal_dir_path()
        self.config = dataclasses.replace(config, dir_path=self._dir_path)
        self._lock = threading.Lock()
        self._writer_id = f"wm-{os.getpid()}-{int(dt.datetime.now().timestamp())}"
        self._segment_index = self._discover_last_segment_index()
        self._sequence = self._discover_last_sequence()
        self._active_path = self._segment_path(self._segment_index)
        self._dir_path.mkdir(parents=True, exist_ok=True)

    @property
    def writer_id(self) -> str:
        return self._writer_id

    def append(
        self,
        *,
        event_type: WalEventType | str,
        wal_run_id: str,
        action_name: str,
        project_path: str,
        trigger: str,
        dev_env: str,
        payload: WalPayload | None = None,
    ) -> None:
        with self._lock:
            self._rotate_if_needed()
            self._sequence += 1
            serialized_event_type = (
                event_type.value if isinstance(event_type, WalEventType) else event_type
            )
            event = {
                "schema_version": 1,
                "sequence": self._sequence,
                "ts": _utc_now_iso(),
                "event_type": serialized_event_type,
                "wal_run_id": wal_run_id,
                "action_name": action_name,
                "project_path": project_path,
                "trigger": trigger,
                "dev_env": dev_env,
                "writer_id": self._writer_id,
                "payload": _serialize_payload(payload),
            }
            with self._active_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=True, default=str))
                handle.write("\n")

    def close(self) -> None:
        return None

    def _rotate_if_needed(self) -> None:
        self._dir_path.mkdir(parents=True, exist_ok=True)
        if not self._active_path.exists():
            return
        if self._active_path.stat().st_size < self.config.max_segment_bytes:
            return
        self._segment_index += 1
        self._active_path = self._segment_path(self._segment_index)
        self._cleanup_old_segments()

    def _cleanup_old_segments(self) -> None:
        segment_paths = self._list_segments()
        if len(segment_paths) <= self.config.max_segments:
            return
        to_remove = segment_paths[: len(segment_paths) - self.config.max_segments]
        for segment_path in to_remove:
            segment_path.unlink(missing_ok=True)

    def _list_segments(self) -> list[pathlib.Path]:
        return sorted(self._dir_path.glob("wal-*.jsonl"))

    def _discover_last_segment_index(self) -> int:
        paths = self._list_segments()
        if not paths:
            return 1
        latest = paths[-1]
        stem = latest.stem  # wal-000001
        try:
            return int(stem.split("-")[1])
        except (IndexError, ValueError):
            return len(paths) + 1

    def _discover_last_sequence(self) -> int:
        paths = self._list_segments()
        if not paths:
            return 0
        latest = paths[-1]
        try:
            lines = latest.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in reversed(lines):
            try:
                payload = json.loads(line)
                sequence = payload.get("sequence")
                if isinstance(sequence, int):
                    return sequence
            except json.JSONDecodeError:
                continue
        return 0

    def _segment_path(self, index: int) -> pathlib.Path:
        return self._dir_path / f"wal-{index:06d}.jsonl"