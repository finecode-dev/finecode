from __future__ import annotations

import finecode_jsonrpc
import pytest

from finecode.wm_server import wm_server
from finecode.wm_server.errors import ActionCancelledError

pytestmark = pytest.mark.anyio


class _FakeWriter:
    """Stand-in for asyncio.StreamWriter — only .drain() is awaited by
    _handle_request_task; actual writes are captured via a monkeypatched
    _write_message instead of a real transport."""

    async def drain(self) -> None:
        return None


async def test_action_cancelled_error_becomes_a_real_jsonrpc_cancelled_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that raises ``errors.ActionCancelledError`` at the WM's TCP
    dispatch boundary must produce a genuine JSON-RPC error response carrying
    the RequestCancelled code (-32800) — not the generic -32603 used for
    ordinary failures, and logged at DEBUG rather than ERROR so a benign
    cancellation never appears as a crash in WM logs or triggers an IDE
    error toast.
    """
    written: list[tuple[object, dict]] = []

    def _capture_write_message(writer: object, msg: dict) -> None:
        written.append((writer, msg))

    monkeypatch.setattr(wm_server, "_write_message", _capture_write_message)

    async def _cancelled_handler(params, ws_context):
        raise ActionCancelledError("cancelled by pyrefly")

    writer = _FakeWriter()
    await wm_server._handle_request_task(
        _cancelled_handler,
        {},
        None,  # ws_context — unused by this handler
        writer,
        42,
        "test-client",
        "actions/run",
    )

    assert len(written) == 1
    _, msg = written[0]
    assert msg["id"] == 42
    assert msg["error"]["code"] == finecode_jsonrpc.REQUEST_CANCELLED
    assert msg["error"]["message"] == "cancelled by pyrefly"
