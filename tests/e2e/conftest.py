"""Shared helpers and fixtures for e2e server lifecycle tests."""

from __future__ import annotations

import os
import shutil
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
    """Deliver a graceful interrupt signal to the entire process group of *proc*.

    On Unix: sends SIGINT to the process group via ``os.killpg``.
    On Windows: sends ``CTRL_C_EVENT`` to the process group.  This requires the
    process to have been started with ``CREATE_NEW_PROCESS_GROUP``, which
    ``subprocess.Popen(..., start_new_session=True)`` guarantees on Windows.
    """
    if sys.platform == "win32":
        os.kill(proc.pid, signal.CTRL_C_EVENT)
    else:
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def kill_group(proc: subprocess.Popen) -> None:
    """Forcefully kill proc and all its descendants (test teardown).

    On Unix: kills the entire process group via ``os.killpg(SIGKILL)`` so
    children that share the group (e.g. WM spawned inside an MCP session)
    are also terminated.
    On Windows: ``os.killpg`` is not available, so the process tree is walked
    with psutil and each member is force-killed individually.
    """
    if sys.platform == "win32":
        try:
            import psutil
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except Exception:
            proc.kill()
    else:
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "test-project"\n'
        'version = "0.1.0"\n'
        '\n'
        '[tool.finecode]\n'
    )
    return tmp_path


@pytest.fixture
def workspace_dir_with_er(tmp_path: Path) -> Path:
    """Workspace with a dev_workspace env copied from the current Python venv.

    Copying (rather than symlinking) gives each test an isolated venv the WM
    can write into (e.g. cache files) without conflicting with parallel tests.
    ``finecode_extension_runner`` is already installed in the active venv, so
    the WM can start a real ER immediately without running prepare-envs.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "test-project"\n'
        'version = "0.1.0"\n'
        '\n'
        "[tool.finecode]\n\n"
        "[[tool.finecode.actions]]\n"
        'name = "test_action"\n\n'
        "[[tool.finecode.actions.handlers]]\n"
        'handler = "finecode_builtin_handlers.DumpConfigHandler"\n'
        'env = "dev_workspace"\n'
    )
    venvs_dir = tmp_path / ".venvs"
    venvs_dir.mkdir()
    current_venv = Path(sys.executable).parent.parent
    shutil.copytree(current_venv, venvs_dir / "dev_workspace", symlinks=True)
    return tmp_path
