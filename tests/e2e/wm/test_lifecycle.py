"""E2E tests for the WM (Workspace Manager) server lifecycle."""

import socket
import subprocess

import pytest

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
            "--port-file", str(port_file),
            "--disconnect-timeout", "5",
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
            "--port-file", str(port_file),
            "--disconnect-timeout", "2",
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
