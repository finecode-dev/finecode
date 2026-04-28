"""E2E tests for the MCP server lifecycle."""

import socket
import subprocess
import time

import pytest

from tests.e2e.conftest import (
    kill_group,
    sigint_group,
    start_server,
    wait_for_file,
    wait_for_port,
)


def test_starts_and_exits_on_sigint(workspace_dir, tmp_path):
    """MCP server starts its WM and exits cleanly on SIGINT.

    Each test uses its own WM instance via ``--wm-port-file`` so tests are
    fully isolated and can run alongside a developer's active IDE session.

    The WM port file appearing proves both that WM is ready and that the MCP
    lifespan completed. Exit code must be 0.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        [
            "start-mcp",
            "--workdir", str(workspace_dir),
            "--wm-port-file", str(port_file),
        ],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file, timeout=20), (
            "MCP/WM server did not start within 20 s — "
            "check that finecode and its dependencies are installed"
        )

        # Give FastMCP a moment to enter mcp.run() after lifespan setup completes
        time.sleep(0.5)

        sigint_group(proc)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("MCP server did not exit within 10 s after SIGINT")
    finally:
        # Kills MCP + WM child if they are still running (e.g. test failed before SIGINT)
        kill_group(proc)

    assert proc.returncode == 0, (
        f"Expected clean exit (0), got {proc.returncode}"
    )


def test_child_wm_dies_on_mcp_sigkill(workspace_dir, tmp_path):
    """WM child process dies when the MCP process is force-killed.

    When an IDE crashes or the process is OOM-killed, the WM spawned by MCP
    must die with it. A surviving WM occupies a port and blocks the next MCP
    startup — a ghost process the user cannot easily discover or stop without
    inspecting the process tree.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        [
            "start-mcp",
            "--workdir", str(workspace_dir),
            "--wm-port-file", str(port_file),
        ],
        cwd=workspace_dir,
    )
    try:
        assert wait_for_file(port_file, timeout=20), (
            "MCP/WM server did not start within 20 s — "
            "check that finecode and its dependencies are installed"
        )

        # Let FastMCP enter mcp.run() after lifespan setup completes.
        time.sleep(0.5)

        wm_port = int(port_file.read_text().strip())
        assert wait_for_port("127.0.0.1", wm_port, timeout=5), (
            f"WM is not accepting connections on port {wm_port}"
        )

        # Force-kill the MCP process group — simulates IDE crash / OOM kill.
        # WM shares this group (start_own_server has no start_new_session),
        # so it must die immediately rather than becoming an orphan.
        kill_group(proc)

        # WM port must become unavailable quickly.
        deadline = time.monotonic() + 5.0
        wm_dead = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", wm_port), timeout=0.3):
                    pass
            except (ConnectionRefusedError, OSError):
                wm_dead = True
                break
            time.sleep(0.2)

        assert wm_dead, (
            f"WM is still accepting connections on port {wm_port} after SIGKILL — "
            "WM may have been started in a separate process group (ghost process risk)"
        )
    finally:
        kill_group(proc)
