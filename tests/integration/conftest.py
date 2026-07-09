"""In-process integration test infra.

Drives the real WM dispatch loop (``wm_server._handle_client``) over a real
TCP loopback connection — no subprocess, no ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from loguru import logger

from finecode.wm_server import context, wm_server
from finecode.wm_server._jsonrpc import _read_message, _write_message


class InProcClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer
        self._pending: dict = {}
        self._notifs: asyncio.Queue = asyncio.Queue()
        self.received_order: list[tuple[str, object]] = []  # ("resp", id) / ("notif", method)
        self._task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await _read_message(self._reader)
                if msg is None:
                    break
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.received_order.append(("resp", msg["id"]))
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif msg.get("method"):
                    self.received_order.append(("notif", msg["method"]))
                    await self._notifs.put(msg)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass

    async def request(self, method: str, params: dict | None = None) -> object:
        req_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        _write_message(
            self._writer,
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        )
        await self._writer.drain()
        msg = await asyncio.wait_for(fut, timeout=5.0)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg.get("result")

    async def next_notification(self, method: str | None = None, timeout: float = 1.0) -> dict:
        async def _get() -> dict:
            while True:
                msg = await self._notifs.get()
                if method is None or msg.get("method") == method:
                    return msg.get("params")

        return await asyncio.wait_for(_get(), timeout=timeout)

    async def close(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
async def wm_client():
    ctx = context.WorkspaceContext([])
    wm_server.reset_log_delivery(interval_ms=20, buffer_limit=5)  # fast + easy overflow
    sink_id = wm_server.install_client_log_sink()
    flush_task = wm_server._start_log_flush_loop()
    srv = await asyncio.start_server(
        lambda r, w: asyncio.ensure_future(wm_server._handle_client(r, w, ctx)),
        "127.0.0.1",
        0,
    )
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = InProcClient(reader, writer)
    try:
        yield client
    finally:
        await client.close()
        flush_task.cancel()
        try:
            await flush_task
        except asyncio.CancelledError:
            pass
        srv.close()
        await srv.wait_closed()
        try:
            logger.remove(sink_id)
        except ValueError:
            pass
        wm_server.reset_log_delivery()


@pytest.fixture
def emit_log_method(monkeypatch: pytest.MonkeyPatch):
    """Register a throwaway logging method through the *real* dispatch table.

    The handler runs on the loop thread, so its ``logger.log`` records are
    delivered synchronously (ADR-0049 §1) and are buffered before the
    per-request force-flush fires — this is what makes the ordering test
    deterministic.
    """

    async def _emit(params: dict | None, ws_context: object) -> dict:
        for level, msg in (params or {}).get("lines", []):
            logger.log(level, msg)
        return {}

    methods = dict(wm_server._METHODS)
    methods["test/emitLog"] = _emit
    monkeypatch.setattr(wm_server, "_METHODS", methods)
