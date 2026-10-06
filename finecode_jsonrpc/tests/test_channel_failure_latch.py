"""Requirement tests: a channel that has failed must fail later requests fast.

REQUIREMENT: once a control RPC has gone unanswered long enough to time out, the
client must not spend another full timeout on the next request. A preset-heavy
recovery issues many preset lookups in a row; without the latch each one would
burn its own timeout against a channel that is provably not answering, so a
single dead ER would stretch a bounded failure into minutes. The latch is also
the signal the recovery-failure reap keys on to retire the runner.
"""

from __future__ import annotations

import time

import pytest

from finecode_jsonrpc import client as jc


def _make_client() -> jc.JsonRpcClient:
    return jc.JsonRpcClient(
        message_types={"test/method": (None, None, str, None)},
        readable_id="test-client",
    )


async def test_channel_fails_fast_after_first_timeout() -> None:
    """The first timeout latches the channel; the next request returns at once."""
    client = _make_client()

    with pytest.raises(jc.ResponseTimeout):
        await client.send_request("test/method", timeout=0.01)

    assert client.channel_failed

    started = time.monotonic()
    with pytest.raises(jc.ServerStoppedError):
        await client.send_request("test/method", timeout=30)
    assert time.monotonic() - started < 1.0
