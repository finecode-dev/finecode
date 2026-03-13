"""WM server lifecycle helpers used by clients.

These functions let any client (LSP server, MCP server, CLI) discover, start,
and wait for the WM server without importing the server implementation itself.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import socket
import subprocess
import sys
import tempfile

from loguru import logger

NO_CLIENT_TIMEOUT_SECONDS = 30


def _cache_dir() -> pathlib.Path:
    """Return the FineCode cache directory inside the dev_workspace venv."""
    return pathlib.Path(sys.executable).parent.parent / "cache" / "finecode"


def discovery_file_path() -> pathlib.Path:
    return _cache_dir() / "wm_port"


def read_port() -> int | None:
    """Read the WM server port from the discovery file. Returns None if not found."""
    path = discovery_file_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def running_port() -> int | None:
    """Return the port if a WM server is actively listening, None otherwise.

    Unlike ``read_port()``, this verifies the server actually accepts connections,
    so a stale discovery file left by a crashed server returns None.
    """
    port = read_port()
    if port is None:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(("127.0.0.1", port))
            return port
    except (ConnectionRefusedError, OSError):
        return None


def is_running() -> bool:
    """Check if a WM server is already listening (discovery file exists and port responds)."""
    return running_port() is not None


def ensure_running(workdir: pathlib.Path, log_level: str = "INFO") -> None:
    """Start the WM server as a subprocess if not already running."""
    if is_running():
        return

    python_cmd = sys.executable
    logger.info(f"Starting FineCode WM server subprocess in {workdir}")
    subprocess.Popen(
        [python_cmd, "-m", "finecode", "start-wm-server", f"--log-level={log_level}"],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_until_ready(timeout: float = 30) -> int:
    """Wait for the WM server to become available. Returns the port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        port = running_port()
        if port is not None:
            return port
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"FineCode WM server did not start within {timeout}s. "
        f"Check logs for errors."
    )


def start_own_server(
    workdir: pathlib.Path,
    log_level: str = "INFO",
    port_file: pathlib.Path | None = None,
) -> pathlib.Path:
    """Start a dedicated WM server subprocess for exclusive use by one client.

    Unlike ``ensure_running()``, this always starts a *fresh* process and writes
    the listening port to a dedicated file (not the shared discovery file), so it
    does not interfere with a concurrently running shared WM server (e.g. the one
    used by the LSP/MCP clients).

    If *port_file* is given the server writes its port there; otherwise a
    temporary file is created automatically.

    Returns the path to the port file.  Pass it to
    ``wait_until_ready_from_file()`` to obtain the port and connect.
    The server auto-stops after the client disconnects.
    """
    if port_file is None:
        fd, port_file_str = tempfile.mkstemp(suffix=".finecode_port")
        os.close(fd)
        port_file = pathlib.Path(port_file_str)
    # Write empty content so the server overwrites rather than appends.
    port_file.write_text("")

    logger.info(f"Starting dedicated FineCode WM server in {workdir}")
    subprocess.Popen(
        [sys.executable, "-m", "finecode", "start-wm-server", "--port-file", str(port_file), "--log-level", log_level],
        cwd=str(workdir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return port_file


async def wait_until_ready_from_file(
    port_file: pathlib.Path, timeout: float = 30
) -> int:
    """Wait for a dedicated WM server using a custom port file. Returns the port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            content = port_file.read_text().strip()
            if content:
                port = int(content)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect(("127.0.0.1", port))
                    return port
        except (FileNotFoundError, ValueError, OSError):
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"Dedicated FineCode WM server did not start within {timeout}s. "
        "Check logs for errors."
    )
