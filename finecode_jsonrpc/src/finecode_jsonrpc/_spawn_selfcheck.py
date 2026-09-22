"""Spawn self-check: prove the WM can spawn a server and read its port.

Runs as ``python -m finecode_jsonrpc._spawn_selfcheck``, from the
``dev_workspace`` venv, before ``prepare-envs`` has created any project env.
It spawns two fake servers through the real production paths -- the TCP port
handshake (``JsonRpcClient``) and the STDIO framing (``StdioTransport``) --
so a spawn regression is caught with a two-line diagnostic instead of a 30s
timeout buried in prepare-envs output.

No pytest import: this module must import in a venv without pytest.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from finecode_jsonrpc import _io_thread, _spawn
from finecode_jsonrpc import client as client_module
from finecode_jsonrpc.transports import StdioTransport

# Fake TCP server: writes its own pid, then binds, publishes the port and blocks
# on accept/recv until the client's force_kill ends it. The pid line must come
# before the port line so the client's startup-window stdout buffer retains it
# (the real ER's port line itself is not buffered).
_FAKE_TCP_SERVER = """\
import os
import socket
print(f"pid={os.getpid()}", flush=True)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen()
port = sock.getsockname()[1]
print(f"Serving on ('127.0.0.1', {port})", flush=True)
conn, _addr = sock.accept()
try:
    while conn.recv(4096):
        pass
finally:
    conn.close()
    sock.close()
"""

# Fake STDIO server: read one Content-Length-framed message and echo its body
# back, framed the same way.
_FAKE_STDIO_SERVER = """\
import sys


def _read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.strip().lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return body


body = _read_message()
if body is not None:
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii") + body
    )
    sys.stdout.buffer.flush()
"""


def argv_for(source: str) -> list[str]:
    return [sys.executable, "-c", source]


async def check_tcp(cmd: _spawn.SpawnCommand, timeout: float) -> tuple[int | None, list[str]]:
    """Spawn a TCP server through ``JsonRpcClient`` and connect to it.

    Returns ``(client.pid, stdout_lines)``; the fake server prints its own pid
    to stdout, so the caller can prove the recorded pid IS the server process
    (no shell wrapper in between).
    """
    io_thread = _io_thread.AsyncIOThread()
    io_thread.start()
    client = client_module.JsonRpcClient(
        message_types={}, readable_id="spawn-selfcheck"
    )
    try:
        await client.start(
            server_cmd=cmd,
            working_dir_path=Path.cwd(),
            io_thread=io_thread,
            debug_port_future=None,
            connect=False,
        )
        await client.connect_to_server(io_thread=io_thread, timeout=timeout)
    finally:
        client.force_kill()
        io_thread.stop(timeout=5.0)
    return client.pid, list(client._stdout_buffer)


async def check_stdio(cmd: _spawn.SpawnCommand, timeout: float) -> None:
    """Spawn an echo server through ``StdioTransport`` and send it one message."""
    transport = StdioTransport(readable_id="spawn-selfcheck")
    received: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_message(message: dict[str, object]) -> None:
        if not received.done():
            received.set_result(message)

    transport.on_message(on_message)
    try:
        await transport.start(cmd, cwd=Path.cwd(), env=None)
        transport.send({"jsonrpc": "2.0", "method": "ping"})
        await asyncio.wait_for(received, timeout=timeout)
    finally:
        await transport.stop()


def main() -> int:
    tcp_source = _FAKE_TCP_SERVER
    timeout = 20.0
    if "--selftest-fail" in sys.argv:
        tcp_source = "import time; time.sleep(60)"
        timeout = 3.0

    try:
        pid, _ = asyncio.run(check_tcp(argv_for(tcp_source), timeout))
    except Exception as exc:  # noqa: BLE001 -- the diagnostic is the point
        print(f"spawn selfcheck: tcp-argv FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"spawn selfcheck: tcp-argv ok (pid {pid})")

    try:
        asyncio.run(check_stdio(argv_for(_FAKE_STDIO_SERVER), timeout))
    except Exception as exc:  # noqa: BLE001 -- the diagnostic is the point
        print(f"spawn selfcheck: stdio-argv FAILED: {exc}", file=sys.stderr)
        return 1
    print("spawn selfcheck: stdio-argv ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())