import os
import socket
import sys

from loguru import logger

import finecode_extension_runner.er_server as extension_runner_er
import finecode_extension_runner.global_state as global_state
from finecode_extension_runner import er_wal


def start_runner_sync(wal_writer: er_wal.ErWalWriter | None = None) -> None:
    assert global_state.project_dir_path is not None

    logger.info(f"Python executable: {sys.executable}")
    logger.info(f"Project path: {global_state.project_dir_path}")
    logger.info(f"Process id: {os.getpid()}")

    server = extension_runner_er.create_er_server(wal_writer=wal_writer)
    # asyncio.run(server.start_io_async())
    port = _find_free_port()
    server.start_tcp(host="127.0.0.1", port=port)


def _find_free_port() -> int:
    """Find and return a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port
