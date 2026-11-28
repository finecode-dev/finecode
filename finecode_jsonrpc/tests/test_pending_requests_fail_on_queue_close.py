"""Requirement tests: a closed inbound channel must fail pending requests.

REQUIREMENT: when the transport stops delivering responses — the inbound
message queue closes — every request still waiting on that channel must fail
promptly with ``ServerStoppedError``. Without this, a caller parks forever on
a future that no one will ever resolve, and a workspace-level operation built
on it (reloading config, restarting a runner) hangs with no log and no error.

A queue close is a *request-liveness* signal, not proof that the OS process
exited: the socket can close before the process (or its shell wrapper) has
finished exiting. So the client must not report the process as stopped here —
that gate belongs to the process watcher, and callers rely on it to avoid
deleting an environment still in use.
"""

from __future__ import annotations

import asyncio

from finecode_jsonrpc import client as jc


def _make_client() -> jc.JsonRpcClient:
    return jc.JsonRpcClient(message_types={}, readable_id="test-client")


async def test_pending_request_fails_when_inbound_queue_closes() -> None:
    """A request in flight when the channel closes fails instead of waiting."""
    client = _make_client()
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    client._async_request_futures["req-1"] = future
    client._expected_result_type_by_msg_id["req-1"] = str

    client.in_message_queue.async_q.put_nowait(jc.QUEUE_END)
    await client.process_incoming_messages()

    assert future.done()
    error = future.exception()
    assert isinstance(error, jc.ServerStoppedError)
    assert not client.server_process_stopped.is_set()
    assert "req-1" not in client._expected_result_type_by_msg_id
