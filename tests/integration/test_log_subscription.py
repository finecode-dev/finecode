"""ADR-0049 — in-process integration tests for WM client log
streaming (``server/subscribeLogs`` / ``server/unsubscribeLogs`` /
``server/logRecords``).

Drives the real dispatch loop over a real TCP loopback connection (see
``tests/integration/conftest.py``); no subprocess, no ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from loguru import logger

from finecode.wm_server import wm_server


async def test_subscribe_then_delivery(wm_client, emit_log_method) -> None:
    """A subscribed connection receives records emitted after it subscribes."""
    result = await wm_client.request("server/subscribeLogs", {"minLevel": "TRACE"})
    assert result == {}

    m = uuid.uuid4().hex
    await wm_client.request("test/emitLog", {"lines": [["INFO", f"hello-{m}"]]})

    params = await wm_client.next_notification("server/logRecords")
    records = params.get("records", [])
    assert any(
        r["message"] == f"hello-{m}" and r["source"] == "wm" and r["level"] == "INFO"
        for r in records
    )


async def test_force_flush_precedes_response(wm_client, emit_log_method) -> None:
    """The buffered log tail is force-flushed (and thus written to the socket)
    before the JSON-RPC response of the request that produced it — otherwise a
    client could see the response before the logs explaining it."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "TRACE"})

    m = uuid.uuid4().hex
    before = len(wm_client.received_order)
    await wm_client.request("test/emitLog", {"lines": [["INFO", f"hello-{m}"]]})
    order_slice = wm_client.received_order[before:]

    notif_indices = [
        i for i, (kind, value) in enumerate(order_slice)
        if kind == "notif" and value == "server/logRecords"
    ]
    resp_indices = [i for i, (kind, _) in enumerate(order_slice) if kind == "resp"]

    assert notif_indices, "expected a server/logRecords notification"
    assert resp_indices, "expected a response to test/emitLog"
    assert notif_indices[0] < resp_indices[0]


async def test_level_filtering(wm_client, emit_log_method) -> None:
    """Records below the subscribed minLevel are never delivered."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "WARNING"})

    m = uuid.uuid4().hex
    skip_message = f"skip-{m}"
    keep_message = f"keep-{m}"
    await wm_client.request(
        "test/emitLog", {"lines": [["INFO", skip_message], ["WARNING", keep_message]]}
    )

    messages: list[str] = []
    try:
        while True:
            params = await wm_client.next_notification("server/logRecords", timeout=0.3)
            messages.extend(r["message"] for r in params.get("records", []))
    except asyncio.TimeoutError:
        pass

    assert keep_message in messages
    assert skip_message not in messages


async def test_redaction_end_to_end(wm_client, emit_log_method) -> None:
    """Sensitive values are redacted before delivery to the client."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    await wm_client.request(
        "test/emitLog", {"lines": [["INFO", f"token=abc123 done-{m}"]]}
    )

    params = await wm_client.next_notification("server/logRecords")
    messages = [r["message"] for r in params.get("records", [])]
    assert f"token=***REDACTED*** done-{m}" in messages


async def test_zero_cost_when_unobserved(wm_client, emit_log_method) -> None:
    """When nobody is subscribed, no server/logRecords notification is ever sent."""
    m = uuid.uuid4().hex
    await wm_client.request("test/emitLog", {"lines": [["INFO", f"unseen-{m}"]]})

    with pytest.raises(asyncio.TimeoutError):
        await wm_client.next_notification("server/logRecords", timeout=0.3)

    assert wm_server._log_registry.has_subscribers() is False


async def test_error_immediacy(wm_client, emit_log_method) -> None:
    """ERROR-level records are flushed immediately, not held for the batch timer."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    await wm_client.request("test/emitLog", {"lines": [["ERROR", f"boom-{m}"]]})

    params = await wm_client.next_notification("server/logRecords", timeout=0.2)
    messages = [r["message"] for r in params.get("records", [])]
    assert f"boom-{m}" in messages


async def test_batched_cadence_via_real_timer(wm_client) -> None:
    """The periodic flush loop (_start_log_flush_loop's tick()) delivers a
    record that was never explicitly flushed by a request or an ERROR level."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    logger.info(f"tick-{m}")

    params = await wm_client.next_notification("server/logRecords", timeout=1.0)
    messages = [r["message"] for r in params.get("records", [])]
    assert f"tick-{m}" in messages


async def test_disconnect_cleanup(wm_client) -> None:
    """When a subscribed client disconnects, the server-side finally block
    unregisters it from the subscription registry."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})
    assert wm_server._log_registry.has_subscribers() is True

    await wm_client.close()

    for _ in range(100):
        if not wm_server._log_registry.has_subscribers():
            break
        await asyncio.sleep(0.01)

    assert wm_server._log_registry.has_subscribers() is False


async def test_dropped_marker(wm_client, emit_log_method) -> None:
    """When the per-connection buffer overflows (fixture uses buffer_limit=5),
    the resulting notification carries a positive droppedCount."""
    await wm_client.request("server/subscribeLogs", {"minLevel": "INFO"})

    m = uuid.uuid4().hex
    lines = [["INFO", f"{m}-{i}"] for i in range(12)]
    await wm_client.request("test/emitLog", {"lines": lines})

    params = await wm_client.next_notification("server/logRecords")
    assert params.get("droppedCount", 0) > 0
