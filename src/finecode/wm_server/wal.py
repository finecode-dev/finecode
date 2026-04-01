from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import pathlib
import sys
import typing
import uuid

from finecode_extension_runner import wal as shared_wal

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
        self._dir_path = config.dir_path or default_wal_dir_path()
        self.config = dataclasses.replace(config, dir_path=self._dir_path)
        self._writer = shared_wal.WalWriter(
            shared_wal.WalConfig(
                enabled=self.config.enabled,
                dir_path=self._dir_path,
                max_segment_bytes=self.config.max_segment_bytes,
                max_segments=self.config.max_segments,
                writer_id_prefix="wm",
            )
        )

    @property
    def writer_id(self) -> str:
        return self._writer.writer_id

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
        serialized_event_type = (
            event_type.value if isinstance(event_type, WalEventType) else event_type
        )
        self._writer.append(
            event_type=serialized_event_type,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_path,
            trigger=trigger,
            dev_env=dev_env,
            payload=_serialize_payload(payload),
        )

    def close(self) -> None:
        self._writer.close()