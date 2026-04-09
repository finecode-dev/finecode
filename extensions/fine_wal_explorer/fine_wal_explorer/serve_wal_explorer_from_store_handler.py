from __future__ import annotations

import asyncio
import dataclasses
import functools
import http.server
import json
import pathlib
import socket
import sys
import threading
import urllib.parse
from typing import Any, Callable, cast

import duckdb
from fine_wal_explorer import store_queries
from finecode_extension_api import code_action
from finecode_extension_api.actions.observability.ingest_wal_to_store_action import (
    IngestWalToStoreAction,
    IngestWalToStoreRunPayload,
    IngestWalToStoreRunResult,
)
from finecode_extension_api.actions.observability.serve_wal_explorer_from_store_action import (
    DEFAULT_WAL_EXPLORER_PORT,
    ServeWalExplorerFromStoreAction,
    ServeWalExplorerFromStoreRunPayload,
    ServeWalExplorerFromStoreRunResult,
)
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iworkspaceactionrunner,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)

SCHEMA_VERSION = 1
_REQUIRED_TABLES = frozenset({"wal_events"})

_STATIC_DIR = pathlib.Path(__file__).parent / "static"
_STATIC_HTML_NAMES = frozenset({"runs", "run", "events", "health"})
_STATIC_ASSETS: dict[str, str] = {
    "style.css": "text/css",
    "plotly.min.js": "application/javascript",
}


@dataclasses.dataclass
class ServeWalExplorerFromStoreHandlerConfig(code_action.ActionHandlerConfig):
    pass


class _BadRequestError(Exception):
    pass


class _ConflictError(Exception):
    pass


class _ServiceUnavailableError(Exception):
    pass


@dataclasses.dataclass
class _ServeState:
    connection: duckdb.DuckDBPyConnection | None
    refreshing: bool = False


class _WalExplorerHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Low-level HTTP handler. Created per request by HTTPServer."""

    _state: _ServeState
    _db_lock: threading.Lock
    _logger: ilogger.ILogger
    _trigger_ingest: Callable[[dict[str, Any]], dict[str, Any]]

    def __init__(
        self,
        state: _ServeState,
        db_lock: threading.Lock,
        logger: ilogger.ILogger,
        trigger_ingest: Callable[[dict[str, Any]], dict[str, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._state = state
        self._db_lock = db_lock
        self._logger = logger
        self._trigger_ingest = trigger_ingest
        super().__init__(*args, **kwargs)

    def _send_static(self, filename: str, content_type: str) -> None:
        """Serve a whitelisted static file. filename must not be user-supplied."""
        file_path = _STATIC_DIR / filename
        try:
            content = file_path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        # Static file routes — whitelist only, no user path passed to filesystem
        if parsed.path == "/":
            self._send_static("index.html", "text/html")
            return
        if parsed.path.startswith("/static/"):
            asset_name = parsed.path[len("/static/") :]
            if asset_name in _STATIC_ASSETS:
                self._send_static(asset_name, _STATIC_ASSETS[asset_name])
            else:
                self._send_json(404, {"error": "not found", "path": parsed.path})
            return
        if parsed.path.endswith(".html"):
            page_name = parsed.path[1:-5]  # strip "/" prefix and ".html" suffix
            if page_name in _STATIC_HTML_NAMES:
                self._send_static(page_name + ".html", "text/html")
            else:
                self._send_json(404, {"error": "not found", "path": parsed.path})
            return

        try:
            if parsed.path == "/health":
                body = self._handle_health()
            elif parsed.path == "/runs":
                body = self._handle_runs(params)
            elif parsed.path == "/timeline":
                body = self._handle_timeline(params)
            elif parsed.path == "/metrics":
                body = self._handle_metrics()
            elif parsed.path == "/events":
                body = self._handle_events(params)
            else:
                self._send_json(404, {"error": "not found", "path": parsed.path})
                return
            self._send_json(200, body)
        except _ServiceUnavailableError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception as exc:
            self._logger.error(f"Request error for {self.path}: {exc}")
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path != "/ingest":
            self._send_json(404, {"error": "not found", "path": parsed.path})
            return

        try:
            request_payload = self._read_json_body()
            response_payload = self._trigger_ingest(request_payload)
            self._send_json(200, response_payload)
        except _BadRequestError as exc:
            self._send_json(400, {"error": str(exc)})
        except _ConflictError as exc:
            self._send_json(409, {"error": str(exc)})
        except _ServiceUnavailableError as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception as exc:
            self._logger.error(f"Request error for {self.path}: {exc}")
            self._send_json(500, {"error": str(exc)})

    def _read_json_body(self) -> dict[str, Any]:
        content_length_raw = self.headers.get("Content-Length", "0")
        try:
            content_length = int(content_length_raw)
        except ValueError as exc:
            raise _BadRequestError("Invalid Content-Length header") from exc

        if content_length <= 0:
            return {}

        raw_body = self.rfile.read(content_length)
        if raw_body == b"":
            return {}

        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise _BadRequestError("Request body must be valid JSON") from exc

        if not isinstance(decoded, dict):
            raise _BadRequestError("JSON body must be an object")

        return decoded

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self._logger.debug("HTTP %s" % (format % args))

    def _handle_health(self) -> dict[str, Any]:
        with self._db_lock:
            return store_queries.get_health(self._require_connection())

    def _handle_runs(self, params: dict[str, list[str]]) -> dict[str, Any]:
        source_id = _first(params, "source_id")
        from_ts = _first(params, "from_ts")
        to_ts = _first(params, "to_ts")
        limit = int(_first(params, "limit") or 1000)
        with self._db_lock:
            runs = store_queries.get_runs(
                self._require_connection(),
                source_id=source_id,
                from_ts_iso=from_ts,
                to_ts_iso=to_ts,
                limit=limit,
            )
        return {"runs": runs}

    def _handle_timeline(self, params: dict[str, list[str]]) -> dict[str, Any]:
        run_id = _first(params, "run_id")
        source_id = _first(params, "source_id")
        from_ts = _first(params, "from_ts")
        to_ts = _first(params, "to_ts")
        event_type = _first(params, "event_type")
        limit = int(_first(params, "limit") or 1000)
        with self._db_lock:
            events = store_queries.get_timeline(
                self._require_connection(),
                run_id=run_id,
                source_id=source_id,
                from_ts_iso=from_ts,
                to_ts_iso=to_ts,
                event_type=event_type,
                limit=limit,
            )
        return {"events": events}

    def _handle_metrics(self) -> dict[str, Any]:
        with self._db_lock:
            return store_queries.get_metrics(self._require_connection())

    def _handle_events(self, params: dict[str, list[str]]) -> dict[str, Any]:
        run_id = _first(params, "run_id")
        source_id = _first(params, "source_id")
        from_ts = _first(params, "from_ts")
        to_ts = _first(params, "to_ts")
        limit = int(_first(params, "limit") or 200)
        with self._db_lock:
            events = store_queries.get_events(
                self._require_connection(),
                run_id=run_id,
                source_id=source_id,
                from_ts_iso=from_ts,
                to_ts_iso=to_ts,
                limit=limit,
            )
        return {"events": events}

    def _require_connection(self) -> duckdb.DuckDBPyConnection:
        if self._state.refreshing:
            raise _ServiceUnavailableError(
                "WAL Explorer is refreshing data; retry shortly"
            )
        if self._state.connection is None:
            raise _ServiceUnavailableError(
                "WAL Explorer database is temporarily unavailable"
            )
        return self._state.connection


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if values:
        return values[0]
    return None


def _build_http_server(
    host: str,
    port: int,
    state: _ServeState,
    db_lock: threading.Lock,
    logger: ilogger.ILogger,
    trigger_ingest: Callable[[dict[str, Any]], dict[str, Any]],
) -> http.server.HTTPServer:
    handler_factory = functools.partial(
        _WalExplorerHTTPHandler,
        state,
        db_lock,
        logger,
        trigger_ingest,
    )
    return http.server.HTTPServer((host, port), handler_factory)


class ServeWalExplorerFromStoreHandler(
    code_action.ActionHandler[
        ServeWalExplorerFromStoreAction,
        ServeWalExplorerFromStoreHandlerConfig,
    ]
):
    def __init__(
        self,
        config: ServeWalExplorerFromStoreHandlerConfig,
        action_runner: iactionrunner.IActionRunner,
        workspace_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.action_runner = action_runner
        self.workspace_runner = workspace_runner
        self.logger = logger

    async def run(
        self,
        payload: ServeWalExplorerFromStoreRunPayload,
        run_context: code_action.RunActionContext[ServeWalExplorerFromStoreRunPayload],
    ):
        store_path = self._resolve_store_path(payload)
        store_path.parent.mkdir(parents=True, exist_ok=True)

        connection = duckdb.connect(str(store_path), read_only=False)
        state = _ServeState(connection=connection)
        db_lock = threading.Lock()
        ingest_lock = threading.Lock()
        server: http.server.HTTPServer | None = None
        server_thread: threading.Thread | None = None
        try:
            self._ensure_schema(connection)
            warnings = self._validate_schema(connection)
            requested_port = self._resolve_bind_port(payload.host, payload.port)

            loop = asyncio.get_running_loop()

            def trigger_ingest(request_payload: dict[str, Any]) -> dict[str, Any]:
                if not ingest_lock.acquire(blocking=False):
                    raise _ConflictError("Ingest already in progress")
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._run_ingest(
                            request_payload=request_payload,
                            store_path=store_path,
                            meta=run_context.meta,
                            state=state,
                            state_lock=db_lock,
                        ),
                        loop,
                    )
                    return future.result()
                finally:
                    ingest_lock.release()

            server = _build_http_server(
                payload.host,
                requested_port,
                state,
                db_lock,
                self.logger,
                trigger_ingest,
            )
            server_thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
                name="wal-explorer-http",
            )
            server_thread.start()

            # Handle both IPv4 (2-tuple) and IPv6 (4-tuple) addresses
            addr_info = server.server_address
            bound_host = addr_info[0]
            bound_port = addr_info[1]
            base_url = f"http://{bound_host}:{bound_port}"
            store_uri = path_to_resource_uri(store_path)

            self.logger.info(
                f"WAL Explorer serving at {base_url} (store: {store_path})"
            )

            # Yield immediately so callers receive address/port before the blocking loop.
            yield ServeWalExplorerFromStoreRunResult(
                schema_version=SCHEMA_VERSION,
                base_url=base_url,
                bound_host=str(bound_host),
                bound_port=int(bound_port),
                store_uri=store_uri,
                warnings=warnings,
            )

            async with run_context.progress("WAL Explorer", cancellable=True) as prog:
                await prog.report(message=f"Serving at {base_url}")
                try:
                    while True:
                        await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    pass
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join()
            with db_lock:
                if state.connection is not None:
                    state.connection.close()
                    state.connection = None

    async def _run_ingest(
        self,
        request_payload: dict[str, Any],
        store_path: pathlib.Path,
        meta: code_action.RunActionMeta,
        state: _ServeState,
        state_lock: threading.Lock,
    ) -> dict[str, Any]:
        since_ts_iso = request_payload.get("since_ts_iso")
        if since_ts_iso is not None and not isinstance(since_ts_iso, str):
            raise _BadRequestError("since_ts_iso must be a string or null")

        project_scope = request_payload.get("project_scope")
        if project_scope is not None and not isinstance(project_scope, str):
            raise _BadRequestError("project_scope must be a string or null")

        self.logger.info("WAL ingest requested from HTTP endpoint")

        ingest_action = self.action_runner.get_action_by_source(IngestWalToStoreAction)
        warnings: list[str] = []
        with state_lock:
            state.refreshing = True
            if state.connection is not None:
                state.connection.close()
                state.connection = None

        try:
            ingest_payload = IngestWalToStoreRunPayload(
                since_ts_iso=since_ts_iso,
                store_uri=path_to_resource_uri(store_path),
                project_scope=cast(ResourceUri | None, project_scope),
            )

            try:
                results_by_project = await self.workspace_runner.run_action_in_projects(
                    action=ingest_action,
                    payload=ingest_payload,
                    meta=meta,
                    concurrently=False,
                )
                ingest_result = IngestWalToStoreRunResult()
                for project_result in results_by_project.values():
                    ingest_result.update(project_result)
            except Exception as exc:
                if not _is_lock_conflict(str(exc)):
                    raise

                self.logger.warning(
                    "Workspace-wide ingest hit DuckDB lock contention; "
                    "falling back to local-project ingest"
                )
                warnings.append(
                    "Workspace-wide ingest hit DuckDB lock contention; "
                    "used local-project ingest fallback."
                )

                # Workspace fan-out can leave a competing writer alive briefly.
                # Retry local fallback until that lock is released.
                local_fallback_attempts = 20
                local_fallback_delay_s = 0.5
                ingest_result = None
                for attempt in range(1, local_fallback_attempts + 1):
                    try:
                        ingest_result = await self.action_runner.run_action(
                            action=ingest_action,
                            payload=ingest_payload,
                            meta=meta,
                        )
                        if attempt > 1:
                            warnings.append(
                                f"Local fallback succeeded after {attempt} attempts."
                            )
                        break
                    except Exception as local_exc:
                        if not _is_lock_conflict(str(local_exc)):
                            raise
                        if attempt == local_fallback_attempts:
                            raise
                        await asyncio.sleep(local_fallback_delay_s)

                if ingest_result is None:
                    raise _ServiceUnavailableError(
                        "Unable to acquire WAL store lock for ingest"
                    ) from None
        finally:
            with state_lock:
                state.connection = duckdb.connect(str(store_path), read_only=False)
                state.refreshing = False

        self.logger.info(
            "WAL ingest completed from HTTP endpoint: "
            f"inserted={ingest_result.events_ingested}, "
            f"duplicates={ingest_result.events_skipped_duplicate}, "
            f"failed={ingest_result.events_failed_parse}"
        )

        return {
            "schema_version": ingest_result.schema_version,
            "store_uri": ingest_result.store_uri,
            "events_ingested": ingest_result.events_ingested,
            "events_skipped_duplicate": ingest_result.events_skipped_duplicate,
            "events_failed_parse": ingest_result.events_failed_parse,
            "first_event_ts_iso": ingest_result.first_event_ts_iso,
            "last_event_ts_iso": ingest_result.last_event_ts_iso,
            "source_summary": [
                {
                    "source_id": item.source_id,
                    "files_scanned": item.files_scanned,
                    "events_read": item.events_read,
                    "events_inserted": item.events_inserted,
                    "events_skipped_duplicate": item.events_skipped_duplicate,
                    "events_failed_parse": item.events_failed_parse,
                }
                for item in ingest_result.source_summary
            ],
            "warnings": ingest_result.warnings + warnings,
        }

    def _resolve_store_path(
        self, payload: ServeWalExplorerFromStoreRunPayload
    ) -> pathlib.Path:
        if payload.store_uri is not None:
            return resource_uri_to_path(payload.store_uri)
        venv_dir_path = pathlib.Path(sys.executable).parent.parent
        return venv_dir_path / "state" / "finecode" / "wal_explorer" / "store.duckdb"

    def _validate_schema(self, connection: duckdb.DuckDBPyConnection) -> list[str]:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }
        missing = _REQUIRED_TABLES - existing
        if missing:
            raise code_action.ActionFailedException(
                f"WAL store is missing required tables: {sorted(missing)}. "
                "Was the store created by ingest_wal_to_store?"
            )
        return []

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

    def _resolve_bind_port(self, host: str, requested_port: int) -> int:
        # Only auto-select a free port when the action default port is requested.
        if requested_port != DEFAULT_WAL_EXPLORER_PORT:
            return requested_port
        if _is_port_available(host, requested_port):
            return requested_port
        return _find_free_port(host)


def _is_port_available(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_free_port(host: str) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _is_lock_conflict(error_message: str) -> bool:
    return (
        "WAL store is locked by another process" in error_message
        or "Could not set lock on file" in error_message
    )
