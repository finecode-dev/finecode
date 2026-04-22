"""E2E tests for the LSP server lifecycle."""

import socket
import subprocess

import pytest

from tests.e2e.conftest import (
    find_free_port,
    kill_group,
    sigint_group,
    start_server,
    wait_for_port,
)


def test_starts_and_exits_on_sigint(workspace_dir):
    """LSP server opens a TCP port on startup and exits cleanly on SIGINT.

    The LSP server only connects to the WM server after receiving the LSP
    ``initialized`` notification from a client, which we never send here.
    This test therefore exercises the LSP server process in isolation.

    Sequence:
        1. Start ``start-lsp --socket <port>`` in TCP mode.
        2. Poll until the port accepts connections — proves the server is up.
        3. Send SIGINT to the process group.
        4. Assert the process exits within 10 s.
    """
    port = find_free_port()

    proc = start_server(
        ["start-lsp", "--socket", str(port)],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_port("127.0.0.1", port), (
            f"LSP server did not open TCP port {port} within 15 s — server failed to start"
        )

        sigint_group(proc)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("LSP server did not exit within 10 s after SIGINT")
    finally:
        kill_group(proc)

    # exit code 0 or 1: LSP server handles KeyboardInterrupt and shuts down cleanly
    assert proc.returncode in (0, 1), (
        f"Expected clean exit (0 or 1), got {proc.returncode}"
    )


def test_exits_on_client_disconnect(workspace_dir):
    """LSP server exits when the TCP client drops the connection without SIGINT.

    This test covers the ghost-process scenario: the IDE closes (or the
    extension crashes) without sending a clean LSP shutdown/exit sequence.
    The server detects the EOF on the TCP reader, shuts down, and exits on its own.

    Sequence:
        1. Start ``start-lsp --socket <port>`` in TCP mode.
        2. Poll until the port accepts connections.
        3. Open a TCP connection (simulates IDE connecting).
        4. Close the socket without sending any LSP messages (simulates IDE crash).
        5. Assert the LSP server process exits within 10 s — no ghost process.
    """
    port = find_free_port()

    proc = start_server(
        ["start-lsp", "--socket", str(port)],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_port("127.0.0.1", port), (
            f"LSP server did not open TCP port {port} within 15 s — server failed to start"
        )

        # Connect and immediately drop — simulates IDE closing without shutdown
        with socket.create_connection(("127.0.0.1", port)):
            pass  # socket closed on __exit__

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "LSP server did not exit within 10 s after client disconnect — "
                "ghost process risk"
            )
    finally:
        kill_group(proc)

    assert proc.returncode in (0, 1), (
        f"Expected clean exit (0 or 1), got {proc.returncode}"
    )
