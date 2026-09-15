"""Requirement tests: a finished request must not stay in the client registry.

REQUIREMENT: once a request stops waiting — succeeded, timed out or
cancelled — its future and expected-result-type entries are removed from the
client. A leaked entry grows without bound across a long-lived client, and a
late response for a finished request must be dropped rather than applied to a
future nobody is waiting on.
"""

from __future__ import annotations

import asyncio

import pytest

from finecode_jsonrpc import client as jc


def _make_client() -> jc.JsonRpcClient:
    return jc.JsonRpcClient(
        message_types={"test/method": (None, None, str, None)},
        readable_id="test-client",
    )


async def test_timed_out_request_cleans_registry_and_drops_late_response() -> None:
    """Timing out mid-wait leaves no registry entry and ignores a late reply."""
    client = _make_client()

    send_task = asyncio.ensure_future(client.send_request("test/method", timeout=0.01))
    await asyncio.sleep(0)
    stale_id = next(iter(client._async_request_futures))

    with pytest.raises(jc.ResponseTimeout):
        await send_task

    assert client._async_request_futures == {}
    assert client._expected_result_type_by_msg_id == {}

    # A response for the finished request must be dropped, not raised out of
    # the message loop.
    await client.handle_message(
        {"jsonrpc": "2.0", "id": stale_id, "result": "late"}
    )
