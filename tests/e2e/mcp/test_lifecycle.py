"""E2E tests for the MCP server lifecycle."""

import os
import signal
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
    """MCP server starts a dedicated WM server and exits cleanly on SIGINT.

    Each test uses its own WM instance via ``--wm-port-file``, so tests are
    fully isolated and can run even when a shared WM server is already running
    (e.g. from a developer's IDE session).

    The MCP server startup sequence:
      1. ``wm_lifecycle.start_own_server(workdir, port_file=...)`` spawns a
         dedicated WM subprocess that writes its port to the given file.
      2. ``wm_lifecycle.wait_until_ready_from_file(port_file)`` polls that file.
      3. ``mcp.run()`` enters the MCP event loop (lifespan connects to WM).

    Because the WM child is spawned inside the MCP process group
    (``start_new_session=True`` on the MCP process, plain ``Popen`` for WM),
    a single ``os.killpg(SIGINT)`` reaches both.

    Sequence:
      1. Start ``start-mcp --workdir <workspace> --wm-port-file <port_file>``.
      2. Poll the per-test port file — proves WM is up and MCP lifespan ran.
      3. Brief pause to let MCP enter its stdio event loop.
      4. Send SIGINT to the process group (hits both MCP and WM child).
      5. Assert MCP process exits within 10 s.
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

    # exit code 0 or 1: MCP / asyncio handles KeyboardInterrupt cleanly
    assert proc.returncode in (0, 1), (
        f"Expected clean exit (0 or 1), got {proc.returncode}"
    )


def test_child_wm_dies_on_mcp_sigkill(workspace_dir, tmp_path):
    """WM child process dies when the MCP process group receives SIGKILL.

    ``start_own_server()`` intentionally does *not* use ``start_new_session``
    when spawning the dedicated WM subprocess, so both MCP and WM share the
    same process group.  A SIGKILL to that group therefore kills both processes
    simultaneously — preventing a ghost WM when the MCP server is force-killed
    (e.g. IDE crash, OOM kill, or ``kill -9``).

    Sequence:
      1. Start the MCP server; it spawns a dedicated WM child.
      2. Poll the per-test WM port file — proves WM is up.
      3. Read the WM port and verify WM is accepting connections.
      4. Send SIGKILL to the MCP process group (hits both MCP and WM child).
      5. Assert the WM port becomes unavailable within 5 s — proves WM died.
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
        os.killpg(proc.pid, signal.SIGKILL)

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
