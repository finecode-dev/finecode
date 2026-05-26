"""Shared append-only WAL writer used by both the WM and ER processes.

On-disk format
--------------
Each WAL is a directory of JSONL segment files (``wal-000001.jsonl``, …).
Every line is one JSON object — a *WAL event* — with the following fields:

Always present
~~~~~~~~~~~~~~
``schema_version``  int   Schema version of this event (see *Version history* below).
``sequence``        int   Monotonically increasing counter within a writer session.
``ts``              str   UTC timestamp in ISO-8601 format.
``event_type``      str   Dot-separated event identifier (e.g. ``"run.accepted"``).
``project_path``    str   Absolute path of the project this event is associated with.
``writer_id``       str   Unique ID of the writer process that produced this event.
``payload``         obj   Event-specific data; always an object, empty ``{}`` when unused.

Present for run events (``run.*``, ``runner.selected``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``wal_run_id``      str   UUID correlating all events for a single action invocation.
``action_name``     str   Fully qualified action name (e.g. ``"lint_files"``).
``trigger``         str   What initiated the run (``"user"``, ``"system"``, …).
``dev_env``         str   Developer environment kind (``"ide"``, ``"cli"``, ``"ai"``, …).

Present for runner lifecycle events (``runner.*``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``env_name``        str   Execution environment name (e.g. ``"dev_no_runtime"``).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import threading
import typing

DEFAULT_MAX_SEGMENT_BYTES = 1_048_576  # 1 MiB
DEFAULT_MAX_SEGMENTS = 20


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclasses.dataclass
class WalConfig:
    enabled: bool = False
    dir_path: pathlib.Path | None = None
    max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES
    max_segments: int = DEFAULT_MAX_SEGMENTS
    writer_id_prefix: str = "writer"


class WalWriter:
    """Append-only JSONL WAL with segment rotation.

    This class intentionally contains only generic writer behavior so different
    processes can define independent event catalogs while sharing durable IO
    guarantees.
    """

    def __init__(self, config: WalConfig) -> None:
        if config.dir_path is None:
            raise ValueError("WalConfig.dir_path must be provided")
        if config.max_segment_bytes <= 0:
            raise ValueError("WalConfig.max_segment_bytes must be > 0")
        if config.max_segments <= 0:
            raise ValueError("WalConfig.max_segments must be > 0")

        self._dir_path = config.dir_path
        self.config = dataclasses.replace(config)
        self._lock = threading.Lock()
        self._writer_id = (
            f"{self.config.writer_id_prefix}-{os.getpid()}-{int(dt.datetime.now().timestamp())}"
        )
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
        event_type: str,
        project_path: str,
        wal_run_id: str | None = None,
        action_name: str | None = None,
        trigger: str | None = None,
        dev_env: str | None = None,
        env_name: str | None = None,
        payload: dict[str, typing.Any] | None = None,
    ) -> None:
        with self._lock:
            self._rotate_if_needed()
            self._sequence += 1
            event: dict[str, typing.Any] = {
                "schema_version": 2,
                "sequence": self._sequence,
                "ts": utc_now_iso(),
                "event_type": event_type,
                "project_path": project_path,
                "writer_id": self._writer_id,
                "payload": payload or {},
            }
            if wal_run_id is not None:
                event["wal_run_id"] = wal_run_id
            if action_name is not None:
                event["action_name"] = action_name
            if trigger is not None:
                event["trigger"] = trigger
            if dev_env is not None:
                event["dev_env"] = dev_env
            if env_name is not None:
                event["env_name"] = env_name
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
                event_payload = json.loads(line)
                sequence = event_payload.get("sequence")
                if isinstance(sequence, int):
                    return sequence
            except json.JSONDecodeError:
                continue
        return 0

    def _segment_path(self, index: int) -> pathlib.Path:
        return self._dir_path / f"wal-{index:06d}.jsonl"