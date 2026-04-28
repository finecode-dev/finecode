"""E2E tests for `finecode run` CLI lifecycle."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

psutil = pytest.importorskip("psutil")

from tests.e2e.conftest import kill_group


def test_wm_exits_after_cli_run_completes(workspace_dir_with_er):
    """The WM server spawned by `finecode run` exits after the action completes.

    When `finecode run` finishes, the WM it started must stop itself. A WM that
    lingers after the CLI exits occupies a port and accumulates across repeated
    `finecode run` invocations — invisible orphans the user cannot easily find.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "finecode", "run", "test_action"],
        cwd=workspace_dir_with_er,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _forward_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    threading.Thread(target=_forward_stderr, daemon=True).start()

    wm_pid: int | None = None
    try:
        # Find the WM child process while finecode run is still alive.
        # start_own_server() spawns the WM without start_new_session, so it
        # inherits the CLI's session and appears as a direct child in psutil.
        parent = psutil.Process(proc.pid)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                children = parent.children(recursive=False)
                wm_procs = [
                    c for c in children
                    if "start-wm-server" in " ".join(c.cmdline())
                ]
                if wm_procs:
                    wm_pid = wm_procs[0].pid
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.5)

        assert wm_pid is not None, (
            "WM server process was not found as a child of `finecode run` within 30 s — "
            "start_own_server() may not be spawning the server as expected"
        )

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("`finecode run` did not complete within 60 s")

        assert proc.returncode in (0, 1), (
            f"Unexpected return code from `finecode run`: {proc.returncode}"
        )

        # WM should auto-exit after the client disconnects.
        # The default disconnect_timeout for start-wm-server is 30 s; allow 40 s total.
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            if not psutil.pid_exists(wm_pid):
                break
            time.sleep(0.5)

        assert not psutil.pid_exists(wm_pid), (
            f"WM server (PID {wm_pid}) is still alive after `finecode run` completed — "
            "disconnect_timeout auto-shutdown is not working in own-server mode"
        )
    finally:
        kill_group(proc)
        if wm_pid is not None:
            try:
                psutil.Process(wm_pid).kill()
            except psutil.NoSuchProcess:
                pass
