from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import pathlib
import sys
import uuid
from typing import Any

import duckdb
from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.ingest_wal_to_store_action import (
    IngestWalToStoreAction,
    IngestWalToStoreRunContext,
    IngestWalToStoreRunPayload,
    IngestWalToStoreRunResult,
    SourceIngestSummary,
    WalSourceSpec,
)
from finecode_extension_api.interfaces import ilogger
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

SCHEMA_VERSION = 1


@dataclasses.dataclass
class IngestWalToStoreHandlerConfig(code_action.ActionHandlerConfig):
    batch_size: int = 1000


class IngestWalToStoreHandler(
    code_action.ActionHandler[
        IngestWalToStoreAction,
        IngestWalToStoreHandlerConfig,
    ]
):
    def __init__(
        self,
        config: IngestWalToStoreHandlerConfig,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.logger = logger

    async def run(
        self,
        payload: IngestWalToStoreRunPayload,
        run_context: IngestWalToStoreRunContext,
    ) -> IngestWalToStoreRunResult:
        source_specs = run_context.source_specs
        if source_specs is None:
            raise code_action.ActionFailedException(
                "source_specs were not populated in run context. "
                "Ensure discovery handler is configured "
                "before ingest handler."
            )

        self._validate_source_specs(source_specs)

        if len(source_specs) == 0:
            store_path = self._resolve_store_path(payload)
            return IngestWalToStoreRunResult(
                schema_version=SCHEMA_VERSION,
                source_summary=[],
                events_ingested=0,
                events_skipped_duplicate=0,
                events_failed_parse=0,
                first_event_ts_iso=None,
                last_event_ts_iso=None,
                store_uri=path_to_resource_uri(store_path),
                warnings=[],
            )

        store_path = self._resolve_store_path(payload)
        store_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            connection = duckdb.connect(str(store_path))
        except Exception as exc:
            msg = str(exc)
            if "Could not set lock on file" in msg:
                raise code_action.ActionFailedException(
                    "WAL store is locked by another process. "
                    f"Close the process currently using {store_path} "
                    "or run the explorer against a different --store_uri. "
                    f"Original DuckDB error: {msg}"
                ) from exc
            raise code_action.ActionFailedException(
                f"Failed to open WAL store at {store_path}: {msg}"
            ) from exc
        try:
            self._ensure_schema(connection)
            ingest_run_id = str(uuid.uuid4())
            now = dt.datetime.now(dt.timezone.utc)
            since_dt = _parse_iso_ts(payload.since_ts_iso)
            self._start_ingest_run(connection, ingest_run_id, now, payload, store_path)

            warnings: list[str] = []
            summaries: list[SourceIngestSummary] = []
            total_inserted = 0
            total_duplicates = 0
            total_failed = 0
            first_ts: dt.datetime | None = None
            last_ts: dt.datetime | None = None

            for spec in source_specs:
                summary = SourceIngestSummary(source_id=spec.source_id)
                source_warnings: list[str] = []
                files = self._discover_files(spec)
                summary.files_scanned = len(files)

                for file_path in files:
                    with file_path.open("r", encoding="utf-8") as handle:
                        for line_no, raw_line in enumerate(handle, start=1):
                            line = raw_line.strip()
                            if line == "":
                                continue
                            summary.events_read += 1

                            try:
                                record = json.loads(line)
                                normalized = _normalize_record(record, spec, file_path, line_no)
                            except Exception as exc:
                                summary.events_failed_parse += 1
                                source_warnings.append(
                                    f"{spec.source_id}:{file_path}:{line_no} parse failed: {exc}"
                                )
                                continue

                            event_ts = normalized["ts"]
                            if since_dt is not None and event_ts is not None and event_ts < since_dt:
                                continue

                            try:
                                inserted = self._insert_event(connection, normalized)
                            except Exception as exc:
                                raise code_action.ActionFailedException(
                                    f"Failed to insert event for source {spec.source_id}: {exc}"
                                ) from exc

                            if inserted:
                                summary.events_inserted += 1
                                if event_ts is not None:
                                    if first_ts is None or event_ts < first_ts:
                                        first_ts = event_ts
                                    if last_ts is None or event_ts > last_ts:
                                        last_ts = event_ts
                            else:
                                summary.events_skipped_duplicate += 1

                summaries.append(summary)
                warnings.extend(source_warnings)

                total_inserted += summary.events_inserted
                total_duplicates += summary.events_skipped_duplicate
                total_failed += summary.events_failed_parse

                self._insert_ingest_source(connection, ingest_run_id, summary)

            self._finish_ingest_run(
                connection,
                ingest_run_id,
                events_ingested=total_inserted,
                events_skipped_duplicate=total_duplicates,
                events_failed_parse=total_failed,
            )

            result = IngestWalToStoreRunResult(
                schema_version=SCHEMA_VERSION,
                source_summary=summaries,
                events_ingested=total_inserted,
                events_skipped_duplicate=total_duplicates,
                events_failed_parse=total_failed,
                first_event_ts_iso=_to_iso(first_ts),
                last_event_ts_iso=_to_iso(last_ts),
                store_uri=path_to_resource_uri(store_path),
                warnings=warnings,
            )
            return result
        finally:
            connection.close()

    def _validate_source_specs(self, source_specs: list[WalSourceSpec]) -> None:
        source_ids = [spec.source_id for spec in source_specs]
        if len(source_ids) != len(set(source_ids)):
            raise code_action.ActionFailedException("source_specs.source_id must be unique")

    def _resolve_store_path(self, payload: IngestWalToStoreRunPayload) -> pathlib.Path:
        if payload.store_uri is not None:
            return resource_uri_to_path(payload.store_uri)

        venv_dir_path = pathlib.Path(sys.executable).parent.parent
        return venv_dir_path / "state" / "finecode" / "wal_explorer" / "store.duckdb"

    def _discover_files(self, spec: WalSourceSpec) -> list[pathlib.Path]:
        source_path = resource_uri_to_path(spec.location_uri)
        include_glob = spec.include_glob or "*.jsonl"
        exclude_glob = spec.exclude_glob

        if source_path.is_file():
            candidates = [source_path]
        elif source_path.is_dir():
            candidates = sorted(source_path.rglob(include_glob))
        else:
            raise code_action.ActionFailedException(
                f"Source location does not exist: {source_path}"
            )

        if exclude_glob is None:
            return candidates

        filtered: list[pathlib.Path] = []
        for path in candidates:
            if fnmatch.fnmatch(path.name, exclude_glob):
                continue
            filtered.append(path)
        return filtered

    def _ensure_schema(self, connection: duckdb.DuckDBPyConnection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wal_events (
                event_hash TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_format TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                ts TIMESTAMP,
                event_type TEXT NOT NULL,
                run_id TEXT,
                action_name TEXT,
                project_path TEXT,
                trigger TEXT,
                dev_env TEXT,
                writer_id TEXT,
                payload_json JSON,
                origin_file_path TEXT,
                origin_line_no BIGINT,
                ingested_at TIMESTAMP NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wal_ingest_runs (
                ingest_run_id TEXT PRIMARY KEY,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                store_uri TEXT,
                since_ts TIMESTAMP,
                events_ingested BIGINT,
                events_skipped_duplicate BIGINT,
                events_failed_parse BIGINT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS wal_ingest_sources (
                ingest_run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                files_scanned BIGINT,
                events_read BIGINT,
                events_inserted BIGINT,
                events_skipped_duplicate BIGINT,
                events_failed_parse BIGINT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_wal_events_run_id_ts ON wal_events(run_id, ts)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_wal_events_source_ts ON wal_events(source_id, ts)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_wal_events_event_type ON wal_events(event_type)"
        )

    def _start_ingest_run(
        self,
        connection: duckdb.DuckDBPyConnection,
        ingest_run_id: str,
        started_at: dt.datetime,
        payload: IngestWalToStoreRunPayload,
        store_path: pathlib.Path,
    ) -> None:
        connection.execute(
            """
            INSERT INTO wal_ingest_runs (
                ingest_run_id,
                started_at,
                store_uri,
                since_ts,
                events_ingested,
                events_skipped_duplicate,
                events_failed_parse
            ) VALUES (?, ?, ?, ?, 0, 0, 0)
            """,
            [
                ingest_run_id,
                started_at,
                str(path_to_resource_uri(store_path)),
                _parse_iso_ts(payload.since_ts_iso),
            ],
        )

    def _insert_event(
        self,
        connection: duckdb.DuckDBPyConnection,
        event: dict[str, Any],
    ) -> bool:
        existing = connection.execute(
            "SELECT 1 FROM wal_events WHERE event_hash = ?",
            [event["event_hash"]],
        ).fetchone()
        if existing is not None:
            return False

        connection.execute(
            """
            INSERT INTO wal_events (
                event_hash,
                source_id,
                source_format,
                schema_version,
                ts,
                event_type,
                run_id,
                action_name,
                project_path,
                trigger,
                dev_env,
                writer_id,
                payload_json,
                origin_file_path,
                origin_line_no,
                ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event["event_hash"],
                event["source_id"],
                event["source_format"],
                event["schema_version"],
                event["ts"],
                event["event_type"],
                event["run_id"],
                event["action_name"],
                event["project_path"],
                event["trigger"],
                event["dev_env"],
                event["writer_id"],
                event["payload_json"],
                event["origin_file_path"],
                event["origin_line_no"],
                event["ingested_at"],
            ],
        )
        return True

    def _insert_ingest_source(
        self,
        connection: duckdb.DuckDBPyConnection,
        ingest_run_id: str,
        summary: SourceIngestSummary,
    ) -> None:
        connection.execute(
            """
            INSERT INTO wal_ingest_sources (
                ingest_run_id,
                source_id,
                files_scanned,
                events_read,
                events_inserted,
                events_skipped_duplicate,
                events_failed_parse
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ingest_run_id,
                summary.source_id,
                summary.files_scanned,
                summary.events_read,
                summary.events_inserted,
                summary.events_skipped_duplicate,
                summary.events_failed_parse,
            ],
        )

    def _finish_ingest_run(
        self,
        connection: duckdb.DuckDBPyConnection,
        ingest_run_id: str,
        events_ingested: int,
        events_skipped_duplicate: int,
        events_failed_parse: int,
    ) -> None:
        connection.execute(
            """
            UPDATE wal_ingest_runs
            SET finished_at = ?,
                events_ingested = ?,
                events_skipped_duplicate = ?,
                events_failed_parse = ?
            WHERE ingest_run_id = ?
            """,
            [
                dt.datetime.now(dt.timezone.utc),
                events_ingested,
                events_skipped_duplicate,
                events_failed_parse,
                ingest_run_id,
            ],
        )


def _normalize_record(
    record: dict[str, Any],
    spec: WalSourceSpec,
    file_path: pathlib.Path,
    line_no: int,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")

    event_type_field = _mapped_field(spec, "event_type", "event_type")
    event_type = record.get(event_type_field)
    if not isinstance(event_type, str) or event_type.strip() == "":
        raise ValueError("missing event_type")

    ts_field = _mapped_field(spec, "ts", "ts")
    parsed_ts = _parse_iso_ts(record.get(ts_field))
    run_id = _read_run_id(record, spec)
    action_name = _coerce_str(record.get(_mapped_field(spec, "action_name", "action_name")))
    payload_value = record.get(_mapped_field(spec, "payload", "payload"), {})
    ingested_at = dt.datetime.now(dt.timezone.utc)
    raw_line = json.dumps(record, sort_keys=True, ensure_ascii=True)
    event_hash_input = (
        f"{spec.source_id}\0{file_path}\0{line_no}\0{raw_line}".encode("utf-8")
    )

    return {
        "event_hash": hashlib.sha256(event_hash_input).hexdigest(),
        "source_id": spec.source_id,
        "source_format": spec.format,
        "schema_version": int(record.get("schema_version", SCHEMA_VERSION)),
        "ts": parsed_ts,
        "event_type": event_type,
        "run_id": run_id,
        "action_name": action_name,
        "project_path": _coerce_str(record.get("project_path")),
        "trigger": _coerce_str(record.get("trigger")),
        "dev_env": _coerce_str(record.get("dev_env")),
        "writer_id": _coerce_str(record.get("writer_id")),
        "payload_json": json.dumps(payload_value, ensure_ascii=True),
        "origin_file_path": str(file_path),
        "origin_line_no": line_no,
        "ingested_at": ingested_at,
    }


def _mapped_field(spec: WalSourceSpec, canonical: str, default: str) -> str:
    if spec.field_mapping is None:
        return default
    mapped = spec.field_mapping.get(canonical)
    if mapped is None:
        return default
    if mapped.strip() == "":
        return default
    return mapped


def _read_run_id(record: dict[str, Any], spec: WalSourceSpec) -> str | None:
    run_field = _mapped_field(spec, "run_id", "wal_run_id")
    return _coerce_str(record.get(run_field))


def _parse_iso_ts(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc)
    if not isinstance(value, str) or value.strip() == "":
        return None

    normalized = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _to_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat()


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
