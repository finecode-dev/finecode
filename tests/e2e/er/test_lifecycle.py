"""E2E tests for Extension Runner lifecycle."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import psutil
import pytest

from tests.e2e.conftest import (
    kill_group,
    sigint_group,
    start_server,
    wait_for_file,
    wait_for_port,
)

# ---------------------------------------------------------------------------
# Minimal WM JSON-RPC client helpers
# ---------------------------------------------------------------------------


def _send_request(sock: socket.socket, method: str, params: dict, req_id: int) -> None:
    """Send a Content-Length-framed JSON-RPC request to the WM server."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    ).encode()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    sock.sendall(header + body)


def _read_response(sock: socket.socket, timeout: float = 30.0) -> dict:
    """Read a single Content-Length-framed JSON-RPC response from the WM server."""
    sock.settimeout(timeout)
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError("Connection closed while reading response header")
        raw += chunk
    header_part, body_start = raw.split(b"\r\n\r\n", 1)
    length = int(header_part.split(b"Content-Length: ")[1])
    body = body_start
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise EOFError("Connection closed while reading response body")
        body += chunk
    return json.loads(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_extension_runners_cleaned_up_on_wm_shutdown(workspace_dir_with_er, tmp_path):
    """Extension Runner subprocesses are terminated when the WM shuts down cleanly.

    When a WM exits, every ER it spawned must be stopped. Orphaned ERs are
    re-parented to PID 1 and silently consume resources — the user cannot
    easily discover or stop them without inspecting the full process tree.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "10",
        ],
        cwd=workspace_dir_with_er,
    )
    er_pids: set[int] = set()
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )

        port = int(port_file.read_text().strip())
        assert wait_for_port("127.0.0.1", port), (
            f"WM server not accepting connections on port {port}"
        )

        with socket.create_connection(("127.0.0.1", port)) as sock:
            # Tell WM to load the workspace directory.  This discovers the
            # project, reads its config, and starts the dev_workspace ER.
            _send_request(
                sock,
                "workspace/addDir",
                {"dirPath": str(workspace_dir_with_er)},
                req_id=1,
            )
            _read_response(sock, timeout=30.0)

            # Poll until the ER child process appears in WM's process tree.
            wm_process = psutil.Process(proc.pid)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    children = wm_process.children(recursive=True)
                    er_procs = [
                        c
                        for c in children
                        if "finecode_extension_runner" in " ".join(c.cmdline())
                    ]
                    if er_procs:
                        er_pids = {p.pid for p in er_procs}
                        break
                except psutil.NoSuchProcess:
                    break
                time.sleep(0.5)

            assert er_pids, (
                "Extension Runner process did not start within 30 s after "
                "workspace/addDir — check that finecode_extension_runner is "
                "installed in the active venv"
            )

        # Connection closed; SIGINT WM before the disconnect timer fires.
        sigint_group(proc)

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("WM did not exit within 15 s after SIGINT")
    finally:
        kill_group(proc)

    # Every ER that was alive before shutdown must be gone now.
    for pid in er_pids:
        assert not psutil.pid_exists(pid), (
            f"Extension Runner PID {pid} is still alive after WM shutdown — "
            "on_shutdown() may not have sent exit to all runners"
        )


@pytest.mark.skipif(
    sys.platform == "win32", reason="stuck-ER wrapper is a POSIX shell script"
)
def test_stuck_er_process_is_killed_when_start_attempt_gives_up(
    workspace_dir_with_stuck_er, tmp_path
):
    """An ER that never reports its port does not outlive the WM giving up on it.

    Before this was fixed, a start attempt that timed out waiting for the ER's
    TCP port (e.g. under resource contention) abandoned the already-spawned OS
    process instead of killing it — nothing ever revisited it, since the WM
    only tracks the runner as FAILED. Under real load this produces exactly
    the kind of unbounded orphaned-process accumulation that starves the
    machine for every future run, not just a single stuck process.
    """
    port_file = tmp_path / "wm_port"

    proc = start_server(
        ["start-wm-server", "--port-file", str(port_file)],
        cwd=workspace_dir_with_stuck_er,
    )
    er_pid: int | None = None
    try:
        assert wait_for_file(port_file), (
            "WM server did not write port file within 15 s — server failed to start"
        )

        port = int(port_file.read_text().strip())
        assert wait_for_port("127.0.0.1", port), (
            f"WM server not accepting connections on port {port}"
        )

        with socket.create_connection(("127.0.0.1", port)) as sock:
            _send_request(
                sock,
                "workspace/addDir",
                {"dirPath": str(workspace_dir_with_stuck_er)},
                req_id=1,
            )

            # The stuck ER is spawned almost immediately, well before the WM's
            # own ~30 s port-wait gives up on it — poll for it so we have a
            # PID to check once that timeout fires.
            wm_process = psutil.Process(proc.pid)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                children = wm_process.children(recursive=True)
                stuck_procs = [c for c in children if "3600" in c.cmdline()]
                if stuck_procs:
                    er_pid = stuck_procs[0].pid
                    break
                time.sleep(0.5)

            assert er_pid is not None, (
                "Stuck Extension Runner process did not appear within 15 s — "
                "check that the bin/python wrapper in workspace_dir_with_stuck_er "
                "is actually intercepting the ER start command"
            )

            # addDir must eventually resolve (error or not) once the WM gives
            # up on the port handshake, rather than hanging forever.
            _read_response(sock, timeout=45.0)

        # By the time addDir's response was sent, the failed start attempt's
        # except-block force_kill() should already have run — give the OS a
        # brief grace window to finish reaping it either way.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and psutil.pid_exists(er_pid):
            time.sleep(0.2)

        assert not psutil.pid_exists(er_pid), (
            f"Stuck Extension Runner PID {er_pid} is still alive after the WM "
            "gave up waiting for its port — the failed start attempt did not "
            "kill the OS process it had already spawned"
        )
    finally:
        kill_group(proc)
