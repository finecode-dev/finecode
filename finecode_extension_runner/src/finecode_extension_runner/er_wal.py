from __future__ import annotations

import dataclasses
import enum
import pathlib
import sys
import typing

from finecode_extension_runner import wal as shared_wal


class ErWalEventType(enum.StrEnum):
    RUN_ACCEPTED = "run.accepted"
    RUN_DISPATCHED = "run.dispatched"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    HANDLER_STARTED = "handler.started"
    HANDLER_COMPLETED = "handler.completed"
    HANDLER_FAILED = "handler.failed"
    HANDLER_PARTS_STARTED = "handler.parts_started"
    HANDLER_PARTS_COMPLETED = "handler.parts_completed"
    PARTIAL_RESULT_FIRST_SENT = "partial_result.first_sent"
    PARTIAL_RESULT_FINAL_SENT = "partial_result.final_sent"


def default_wal_dir_path() -> pathlib.Path:
    venv_dir_path = pathlib.Path(sys.executable).parent.parent
    return venv_dir_path / "state" / "finecode" / "wal" / "er"


def _serialize_payload(payload: typing.Any | None) -> dict[str, typing.Any]:
    if payload is None:
        return {}
    if dataclasses.is_dataclass(payload):
        return dataclasses.asdict(payload)
    if isinstance(payload, dict):
        return payload
    return {"value": str(payload)}


@dataclasses.dataclass
class ErWalConfig:
    dir_path: pathlib.Path | None = None
    max_segment_bytes: int = shared_wal.DEFAULT_MAX_SEGMENT_BYTES
    max_segments: int = shared_wal.DEFAULT_MAX_SEGMENTS


class ErWalWriter:
    def __init__(self, config: ErWalConfig | None = None) -> None:
        effective_config = config or ErWalConfig()
        self._dir_path = effective_config.dir_path or default_wal_dir_path()
        self._writer = shared_wal.WalWriter(
            shared_wal.WalConfig(
                enabled=True,
                dir_path=self._dir_path,
                max_segment_bytes=effective_config.max_segment_bytes,
                max_segments=effective_config.max_segments,
                writer_id_prefix="er",
            )
        )

    def append(
        self,
        *,
        event_type: ErWalEventType | str,
        wal_run_id: str,
        action_name: str,
        project_path: str,
        trigger: str,
        dev_env: str,
        payload: typing.Any | None = None,
    ) -> None:
        serialized_event_type = (
            event_type.value if isinstance(event_type, ErWalEventType) else event_type
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


def emit_run_event(
    wal_writer: ErWalWriter | None,
    *,
    event_type: ErWalEventType,
    wal_run_id: str,
    action_name: str,
    project_path: pathlib.Path | str,
    trigger: str,
    dev_env: str,
    payload: typing.Any | None = None,
) -> None:
    if wal_writer is None:
        return
    wal_writer.append(
        event_type=event_type,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=str(project_path),
        trigger=trigger,
        dev_env=dev_env,
        payload=payload,
    )
