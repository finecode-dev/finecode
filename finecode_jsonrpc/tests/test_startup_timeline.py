"""Requirement tests (R4): the startup timeline must explain a stalled boot.

REQUIREMENT: when a server never publishes its port, the failure diagnostic must
say whether the server produced any output at all and when each startup milestone
happened. "Empty stdout" and "output but no port line" point at different causes,
and a bare timeout cannot distinguish them.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from finecode_jsonrpc import _io_thread
from finecode_jsonrpc import client as jc


async def test_read_stdout_records_first_output_and_port_line() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"booting handler package\n")
    reader.feed_data(b"Serving on ('127.0.0.1', 1234)\n")
    reader.feed_eof()
    port_future: asyncio.Future = asyncio.get_running_loop().create_future()
    timeline = jc.StartupTimeline(spawned_at=time.monotonic())

    await jc.read_stdout(
        reader, threading.Event(), port_future, 4242, None, None, timeline=timeline
    )

    assert timeline.first_output_at is not None
    assert timeline.port_line_at is not None
    assert timeline.port_line_at >= timeline.first_output_at


async def test_log_stderr_records_first_output() -> None:
    """A failure that only writes to stderr (an import error, a traceback) must
    still count as output the server produced."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"ModuleNotFoundError: no module named 'ruff'\n")
    reader.feed_eof()
    timeline = jc.StartupTimeline(spawned_at=time.monotonic())

    await jc.log_stderr(reader, threading.Event(), None, timeline=timeline)

    assert timeline.first_output_at is not None


def test_describe_reports_no_output_and_no_port_line() -> None:
    """A spawned server that produced nothing and published no port must render
    both absences explicitly, so the timeout message is unambiguous."""
    timeline = jc.StartupTimeline(spawned_at=100.0)

    described = timeline.describe(130.0)

    assert "no output from the server" in described
    assert "no port line" in described


def test_describe_reports_the_milestones_with_their_durations() -> None:
    timeline = jc.StartupTimeline(
        spawned_at=100.0, first_output_at=100.4, port_line_at=129.8
    )

    described = timeline.describe(130.0)

    assert "spawned 30.0s ago" in described
    assert "first output after 0.4s" in described
    assert "port line after 29.8s" in described


async def test_port_timeout_message_carries_the_process_snapshot() -> None:
    """A port-timeout failure names the still-running spawned server by its
    processes, on every platform, so the diagnostic can distinguish "working,
    slowly" from "already exited" on macOS and Windows as well as Linux."""
    io_thread = _io_thread.AsyncIOThread()
    io_thread.start()
    client = jc.JsonRpcClient(message_types={}, readable_id="timeout-test")
    try:
        await client.start(
            server_cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            working_dir_path=Path.cwd(),
            io_thread=io_thread,
            debug_port_future=None,
            connect=False,
        )
        with pytest.raises(jc.ServerFailedToStart) as raised:
            await client.connect_to_server(io_thread=io_thread, timeout=0.5)
    finally:
        client.force_kill()
        io_thread.stop(timeout=5.0)

    label = "\nServer processes at timeout:\n"
    assert label in raised.value.message
    snapshot = raised.value.message.split(label, 1)[1]
    # The snapshot lists the still-living spawned python as its first line.
    assert snapshot.startswith(f"{client.pid} ")
