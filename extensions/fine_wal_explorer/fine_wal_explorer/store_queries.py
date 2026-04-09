from __future__ import annotations

import json
from typing import Any

import duckdb

SCHEMA_VERSION = 1


def _ts_to_iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _decode_payload_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        decoded = json.loads(stripped)
        if isinstance(decoded, dict):
            return decoded
        return None
    return None


def get_health(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    total_events = conn.execute("SELECT COUNT(*) FROM wal_events").fetchone()[0]
    total_runs = conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM wal_events WHERE run_id IS NOT NULL"
    ).fetchone()[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "total_events": int(total_events),
        "total_runs": int(total_runs),
    }


def get_runs(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str | None = None,
    from_ts_iso: str | None = None,
    to_ts_iso: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    conditions = ["run_id IS NOT NULL"]
    params: list[Any] = []
    if source_id is not None:
        conditions.append("source_id = ?")
        params.append(source_id)
    if from_ts_iso is not None:
        conditions.append("ts >= ?")
        params.append(from_ts_iso)
    if to_ts_iso is not None:
        conditions.append("ts <= ?")
        params.append(to_ts_iso)

    params.append(limit)
    sql = f"""
        SELECT
            run_id,
            source_id,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            epoch_ms(MAX(ts)) - epoch_ms(MIN(ts)) AS duration_ms,
            COUNT(*) AS event_count,
            ANY_VALUE(action_name) AS action_name,
            BOOL_OR(event_type LIKE '%.failed' OR event_type LIKE '%.error') AS has_failed,
            BOOL_OR(event_type LIKE '%.completed') AS has_completed
        FROM wal_events
        WHERE {' AND '.join(conditions)}
        GROUP BY run_id, source_id
        ORDER BY first_ts DESC NULLS LAST
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        has_failed = bool(row[7])
        has_completed = bool(row[8])
        status = "incomplete"
        if has_failed:
            status = "failed"
        elif has_completed:
            status = "success"

        result.append(
            {
                "run_id": row[0],
                "source_id": row[1],
                "action_name": row[6],
                "first_ts_iso": _ts_to_iso(row[2]),
                "last_ts_iso": _ts_to_iso(row[3]),
                "duration_ms": row[4],
                "event_count": int(row[5]),
                "status": status,
            }
        )
    return result


def _query_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str | None,
    source_id: str | None,
    from_ts_iso: str | None,
    to_ts_iso: str | None,
    event_type: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    conditions = ["1=1"]
    params: list[Any] = []
    if run_id is not None:
        conditions.append("run_id = ?")
        params.append(run_id)
    if source_id is not None:
        conditions.append("source_id = ?")
        params.append(source_id)
    if from_ts_iso is not None:
        conditions.append("ts >= ?")
        params.append(from_ts_iso)
    if to_ts_iso is not None:
        conditions.append("ts <= ?")
        params.append(to_ts_iso)
    if event_type is not None:
        conditions.append("event_type = ?")
        params.append(event_type)

    params.append(limit)
    sql = f"""
        SELECT
            event_hash, source_id, ts, event_type, run_id, action_name,
            project_path, trigger, dev_env, writer_id, payload_json,
            origin_file_path, origin_line_no
        FROM wal_events
        WHERE {' AND '.join(conditions)}
        ORDER BY ts ASC NULLS LAST
        LIMIT ?
    """
    rows = conn.execute(sql, params).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        events.append(
            {
                "event_hash": row[0],
                "source_id": row[1],
                "ts": _ts_to_iso(row[2]),
                "event_type": row[3],
                "run_id": row[4],
                "action_name": row[5],
                "project_path": row[6],
                "trigger": row[7],
                "dev_env": row[8],
                "writer_id": row[9],
                "payload_json": _decode_payload_json(row[10]),
                "origin_file_path": row[11],
                "origin_line_no": row[12],
            }
        )
    return events


def get_timeline(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str | None = None,
    source_id: str | None = None,
    from_ts_iso: str | None = None,
    to_ts_iso: str | None = None,
    event_type: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    return _query_events(
        conn,
        run_id=run_id,
        source_id=source_id,
        from_ts_iso=from_ts_iso,
        to_ts_iso=to_ts_iso,
        event_type=event_type,
        limit=limit,
    )


def get_metrics(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = conn.execute(
        """
        WITH run_durations AS (
            SELECT
                run_id,
                epoch_ms(MAX(ts)) - epoch_ms(MIN(ts)) AS duration_ms,
                BOOL_OR(event_type LIKE '%.failed' OR event_type LIKE '%.error') AS has_failed
            FROM wal_events
            WHERE run_id IS NOT NULL
            GROUP BY run_id
        )
        SELECT
            (SELECT COUNT(*) FROM wal_events) AS total_events,
            COUNT(*) AS total_runs,
            COALESCE(SUM(has_failed::INT), 0) AS failed_runs,
            QUANTILE_CONT(duration_ms, 0.5) AS p50_duration_ms,
            QUANTILE_CONT(duration_ms, 0.95) AS p95_duration_ms
        FROM run_durations
        """
    ).fetchone()
    return {
        "total_events": int(row[0]),
        "total_runs": int(row[1]),
        "failed_runs": int(row[2]),
        "p50_duration_ms": row[3],
        "p95_duration_ms": row[4],
    }


def get_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str | None = None,
    source_id: str | None = None,
    from_ts_iso: str | None = None,
    to_ts_iso: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return _query_events(
        conn,
        run_id=run_id,
        source_id=source_id,
        from_ts_iso=from_ts_iso,
        to_ts_iso=to_ts_iso,
        event_type=None,
        limit=limit,
    )
