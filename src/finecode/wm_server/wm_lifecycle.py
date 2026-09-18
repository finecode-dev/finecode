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
import time
import typing

from filelock import FileLock
from loguru import logger

NO_CLIENT_TIMEOUT_SECONDS = 30
STARTUP_LOCK_FILENAME = "wm_start.lock"
STARTUP_READY_TIMEOUT_SECONDS = 10.0
STARTUP_READY_POLL_INTERVAL_SECONDS = 0.1


def _cache_dir() -> pathlib.Path:
    """Return the FineCode cache directory inside the dev_workspace venv."""
    return pathlib.Path(sys.executable).parent.parent / "cache" / "finecode"


def discovery_file_path() -> pathlib.Path:
    return _cache_dir() / "wm_port"


def startup_stderr_log_path() -> pathlib.Path:
    return _cache_dir() / "wm_startup_stderr.log"


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


def _startup_lock() -> FileLock:
    """Serialize shared WM startup across processes."""
    lock_path = _cache_dir() / STARTUP_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_path))


def ensure_running(
    workdir: pathlib.Path,
    log_level: str = "INFO",
    keep_alive: bool = False,
    disconnect_timeout: int | None = None,
    wal_enabled: bool = False,
) -> None:
    """Start the WM server as a subprocess if not already running.

    *keep_alive* asks a server started *here* to disable its auto-stop timers.
    It is passed explicitly rather than read from the environment: the shared
    server is started by whichever client gets there first, and an ambient
    setting would be inherited by dedicated servers too, which must stop.

    A server that is already listening is left alone, whatever settings it was
    started with — this ensures *a* server, not one configured like this.
    """
    with _startup_lock():
        if is_running():
            return

        python_cmd = sys.executable
        stderr_path = startup_stderr_log_path()
        logger.info(f"Starting FineCode WM server subprocess in {workdir}")
        command = [
            python_cmd,
            "-m",
            "finecode",
            "start-wm-server",
            f"--log-level={log_level}",
        ]
        if keep_alive:
            command.append("--keep-alive")
        if disconnect_timeout is not None:
            command.append(f"--disconnect-timeout={disconnect_timeout}")
        if wal_enabled:
            command.append("--wal")
        with open(stderr_path, "w") as stderr_file:
            subprocess.Popen(
                command,
                cwd=str(workdir),
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                # Own session: the shared server must outlive whichever client
                # happened to start it, so it must not take that client's
                # signals. The disconnect timer is what keeps it from becoming
                # an orphan.
                start_new_session=True,
            )

        # Keep the lock until the spawned server is observable via discovery and
        # TCP probe so competing callers do not start a duplicate process.
        deadline = time.monotonic() + STARTUP_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if is_running():
                return
            time.sleep(STARTUP_READY_POLL_INTERVAL_SECONDS)


async def wait_until_ready(timeout: float = 30) -> int:
    """Wait for the WM server to become available. Returns the port."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # In a thread: `running_port` probes with a synchronous connect that
        # takes its full timeout when the port is filtered rather than refused.
        port = await asyncio.to_thread(running_port)
        if port is not None:
            return port
        await asyncio.sleep(0.5)
    raise TimeoutError(
        f"FineCode WM server did not start within {timeout}s. Check logs for errors."
    )


def start_own_server(
    workdir: pathlib.Path,
    log_level: str = "INFO",
    port_file: pathlib.Path | None = None,
    wal_enabled: bool = False,
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

    stderr_path = startup_stderr_log_path()
    # Unlike `ensure_running`, nothing else guarantees this directory exists
    # first (there `_startup_lock()` creates it as a side effect before the
    # log file is opened) — on a first-ever start in this venv it is missing.
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Starting dedicated FineCode WM server in {workdir}")
    command = [
        sys.executable,
        "-m",
        "finecode",
        "start-wm-server",
        "--port-file",
        str(port_file),
        "--log-level",
        log_level,
    ]
    if wal_enabled:
        command.append("--wal")

    # No own session, unlike `ensure_running`: a dedicated server belongs to
    # exactly one client and is reachable only through that client's port file,
    # so outliving it would leave a ghost nobody can find. stderr is still
    # captured to a file (not DEVNULL'd) so a crash before the server's own
    # logger is initialized is not silently lost — see `wait_until_ready_from_file`,
    # whose error message points here.
    with open(stderr_path, "w") as stderr_file:
        subprocess.Popen(
            command,
            cwd=str(workdir),
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
    return port_file


async def replace_running_server(
    client: typing.Any,
    workdir: pathlib.Path,
    log_level: str = "INFO",
    timeout: float = 30,
) -> dict:
    """Stop the running WM server, start a replacement, and re-attach *client*.

    The only recovery operation that is not a WM method (ADR-0077 rule 1): a
    server cannot define its own replacement. It lives in shared client
    infrastructure rather than in one surface, which is what rule 2 — one
    definition, not one per client — actually asks for.

    ``client`` reconnects through its own configured reconnect policy
    (ADR-0074), so it must have one; that is also what re-attaches its session.

    Raises:
        TimeoutError: the previous server did not stop, the replacement did not
            start, or the client did not re-attach within *timeout*.
    """
    previous_port = read_port()
    # Captured before the shutdown: the current connection reads as live until
    # its reader notices the server is gone, so "connected" alone would be
    # satisfied by the very connection being replaced.
    epoch = client.connection_epoch
    logger.info(f"Replacing FineCode WM server (port {previous_port})")
    try:
        await client.request("server/shutdown")
    except (ConnectionError, OSError) as exception:
        # The server may close the socket before its response is read; it is
        # stopping either way, which is what the poll below actually verifies.
        logger.debug(f"WM server closed the connection on shutdown: {exception}")

    # The server removes its discovery file and stops accepting new connections
    # before the ones it is already serving end, and it does not exit while a
    # client is still attached. So the file going away proves nothing about the
    # server this client is talking to — dropping the connection is both how the
    # old process is allowed to exit and how this client starts coming back.
    await client.drop_connection()

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # In a thread: `running_port` probes with a synchronous connect that
        # takes its full timeout when the port is filtered rather than refused,
        # and this poll runs on the caller's event loop.
        current_port = await asyncio.to_thread(running_port)
        # A *different* port listening means the old server stopped and a
        # replacement is already up: the client's own reconnect may start one
        # (its policy allows it) before this poll ever observes the gap, and
        # waiting for a `None` that has already been and gone would time out
        # over a workspace that is healthy.
        if current_port is None or current_port != previous_port:
            break
        await asyncio.sleep(STARTUP_READY_POLL_INTERVAL_SECONDS)
    else:
        raise TimeoutError(
            f"FineCode WM server on port {previous_port} did not stop within {timeout}s"
        )

    await asyncio.to_thread(ensure_running, workdir, log_level)
    port = await wait_until_ready(timeout=timeout)
    await client.wait_connected(timeout=timeout, after_epoch=epoch)
    logger.info(f"FineCode WM server replaced: port {previous_port} -> {port}")
    return {"previousPort": previous_port, "port": port}


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
    stderr_path = startup_stderr_log_path()
    # Inlined, not just referenced by path: on CI the runner's disk is gone by
    # the time anyone could go look, so the message itself is the only place
    # this content is ever seen.
    try:
        stderr_tail = stderr_path.read_text().strip()
    except OSError:
        stderr_tail = ""
    detail = (
        f"Captured stderr ({stderr_path}):\n{stderr_tail}"
        if stderr_tail
        else f"{stderr_path} is empty — the process is still starting, not crashing."
    )
    raise TimeoutError(
        f"Dedicated FineCode WM server did not start within {timeout}s.\n{detail}"
    )
