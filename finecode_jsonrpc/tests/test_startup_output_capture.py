"""Requirement / edge-case tests: ER startup output must be capturable for diagnostics.

REQUIREMENT (ADR-0049 edge case): when an Extension Runner fails to start or exits
before publishing its TCP port, there is no RPC channel, so the ``er/logRecords``
forwarding cannot run. The ER writes its own loguru diagnostics to *stdout*, which the
client otherwise reads only to extract the ``Serving on (...)`` port line and then
discards. To keep a start failure legible over the protocol, ``read_stdout`` retains the
startup-window stdout (bounded) so a ``ServerFailedToStart`` diagnostic can surface it
(alongside the captured stderr tail).

These tests pin that behaviour so the actionable output is never silently dropped again.
"""
from __future__ import annotations

import asyncio
import threading

from finecode_jsonrpc import client as jc


async def _run_read_stdout(lines: list[bytes]) -> tuple[list[str], asyncio.Future]:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    port_future: asyncio.Future = asyncio.get_running_loop().create_future()
    buffer: list[str] = []
    await jc.read_stdout(reader, threading.Event(), port_future, 4242, None, buffer)
    return buffer, port_future


async def test_startup_stdout_is_captured_until_port_is_published() -> None:
    buffer, port_future = await _run_read_stdout(
        [
            b"ER: importing handler package fine_python_ruff\n",
            b"ER: ModuleNotFoundError: no module named 'ruff'\n",
            b"Serving on ('127.0.0.1', 12345)\n",
            b"post-port line must not be captured\n",
        ]
    )
    # The port is still parsed as before.
    assert port_future.done() and port_future.result() == 12345
    # Pre-port diagnostics are retained — this is the actionable startup-failure output.
    assert "ER: importing handler package fine_python_ruff" in buffer
    assert any("ModuleNotFoundError" in line for line in buffer)
    # Capture stops once the port is published (channel is up; er/logRecords takes over).
    assert all("post-port" not in line for line in buffer)
    # The port marker line itself is not stored as a diagnostic line.
    assert all("Serving on" not in line for line in buffer)


async def test_startup_stdout_buffer_is_bounded() -> None:
    # No port line: every line is "pre-port" and eligible for capture — must stay bounded
    # so a chatty/looping ER cannot grow WM memory before it ever comes up.
    total = jc._STDOUT_STARTUP_BUFFER_MAX + 50
    buffer, port_future = await _run_read_stdout(
        [f"line {i}\n".encode() for i in range(total)]
    )
    assert not port_future.done()
    assert len(buffer) == jc._STDOUT_STARTUP_BUFFER_MAX
    # Oldest lines dropped, newest kept.
    assert buffer[-1] == f"line {total - 1}"
    assert buffer[0] == f"line {total - jc._STDOUT_STARTUP_BUFFER_MAX}"


async def test_stdout_capture_is_opt_in() -> None:
    # With no buffer supplied, read_stdout behaves exactly as before: parses the port,
    # captures nothing.
    reader = asyncio.StreamReader()
    reader.feed_data(b"some diagnostic\n")
    reader.feed_data(b"Serving on ('127.0.0.1', 999)\n")
    reader.feed_eof()
    port_future: asyncio.Future = asyncio.get_running_loop().create_future()
    await jc.read_stdout(reader, threading.Event(), port_future, 1, None, None)
    assert port_future.result() == 999
