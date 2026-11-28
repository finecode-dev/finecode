"""E2E tests for the WM (Workspace Manager) server lifecycle."""

import socket
import subprocess
import time

import pytest

from finecode.wm_server.wm_server import NO_CLIENT_TIMEOUT_SECONDS
from tests.e2e.conftest import (
    kill_group,
    sigint_group,
    start_server,
    wait_for_file,
    wait_for_port,
)


def test_starts_and_exits_on_sigint(workspace_dir):
    """WM server starts, responds to SIGINT, and cleans up resources.

    Verifies that the WM server writes its port file on startup, exits cleanly on SIGINT, and removes the port file on shutdown.
    """
    port_file = workspace_dir / "wm_port"

    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "5",
        ],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )

        sigint_group(proc)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("WM server did not exit within 10 s after SIGINT")
    finally:
        kill_group(proc)

    # exit code 0 or 1: clean shutdown via KeyboardInterrupt / asyncio cancellation
    assert proc.returncode in (0, 1), (
        f"Expected clean exit (0 or 1), got {proc.returncode}"
    )
    assert not port_file.exists(), (
        "Port file was not removed — stop() may not have run in the finally block"
    )


def test_auto_shutdown_after_disconnect_timeout(workspace_dir, tmp_path):
    """WM server auto-shuts down after all clients disconnect.

    Verifies that the WM server exits on its own after the last TCP client disconnects and removes the port file as part of cleanup.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "2",
        ],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )

        port = int(port_file.read_text().strip())
        assert wait_for_port("127.0.0.1", port), (
            f"WM server not accepting connections on port {port}"
        )

        # Connect and immediately close — triggers the 2-second disconnect timer.
        with socket.create_connection(("127.0.0.1", port)):
            pass  # socket closed on __exit__

        try:
            proc.wait(timeout=7)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "WM server did not exit within 7 s after client disconnect — "
                "disconnect-timeout auto-shutdown may not be working"
            )
    finally:
        kill_group(proc)

    assert not port_file.exists(), (
        "Port file was not removed after WM auto-shutdown — "
        "stop() cleanup may not have run"
    )


def test_keep_alive_survives_client_disconnect(workspace_dir, tmp_path):
    """--keep-alive suppresses the disconnect auto-stop.

    The disconnect timeout is what makes a shared server throw away its loaded
    config and started runners between two CLI calls. A server whose lifetime
    something else owns (the devcontainer) must keep them.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "2",
            "--keep-alive",
        ],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )

        port = int(port_file.read_text().strip())
        assert wait_for_port("127.0.0.1", port), (
            f"WM server not accepting connections on port {port}"
        )

        # Connect and immediately close — would trigger the 2-second disconnect
        # timer on a server without --keep-alive.
        with socket.create_connection(("127.0.0.1", port)):
            pass

        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=6)

        assert wait_for_port("127.0.0.1", port), (
            "WM server stopped accepting connections after the last client "
            "disconnected — --keep-alive did not suppress the disconnect auto-stop"
        )
    finally:
        kill_group(proc)


def test_keep_alive_survives_no_client_timeout(workspace_dir, tmp_path):
    """--keep-alive suppresses the never-had-a-client auto-stop.

    A server autostarted at container start has no client at all until someone
    runs a command, which may be much later than this timeout.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        ["start-wm-server", "--port-file", str(port_file), "--keep-alive"],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )
        port = int(port_file.read_text().strip())

        # No client connects for the whole window — the only thing under test.
        time.sleep(NO_CLIENT_TIMEOUT_SECONDS + 3)

        assert proc.poll() is None, (
            f"WM server exited within {NO_CLIENT_TIMEOUT_SECONDS + 3} s with no "
            "client — --keep-alive did not suppress the no-client auto-stop"
        )
        assert wait_for_port("127.0.0.1", port), (
            "WM server is no longer accepting connections"
        )
    finally:
        kill_group(proc)
