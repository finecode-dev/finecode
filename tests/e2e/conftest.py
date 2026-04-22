"""Shared helpers and fixtures for e2e server lifecycle tests."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    """Bind to port 0 and return the assigned port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_file(path: Path, timeout: float = 15.0) -> bool:
    """Poll until *path* exists and is non-empty."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.2)
    return False


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until a TCP connection to *host:port* succeeds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def start_server(args: list[str], cwd: Path) -> subprocess.Popen:
    """Launch ``python -m finecode <args>`` in an isolated process group.

    Using ``start_new_session=True`` ensures the server and any subprocesses
    it spawns (e.g. WM server started by the MCP server) share a dedicated
    process group, so ``sigint_group`` / ``kill_group`` can reach all of them.

    Stderr is forwarded to the parent process (captured by pytest and shown on
    failure) via a background thread so the pipe buffer never blocks the child.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "finecode"] + args,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _forward_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    threading.Thread(target=_forward_stderr, daemon=True).start()
    return proc


def sigint_group(proc: subprocess.Popen) -> None:
    """Send SIGINT to the entire process group of *proc*."""
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        pass


def kill_group(proc: subprocess.Popen) -> None:
    """Forcefully kill the entire process group of *proc* (test teardown)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wm_shared_port_file() -> Path:
    """Return the shared WM discovery file path for the active Python venv.

    Mirrors the formula in ``wm_server._cache_dir()``:
    ``Path(sys.executable).parent.parent / "cache" / "finecode" / "wm_port"``
    """
    return Path(sys.executable).parent.parent / "cache" / "finecode" / "wm_port"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Minimal FineCode workspace with an empty pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("[tool.finecode]\n")
    return tmp_path
