# docs: docs/cli.md
from __future__ import annotations

import socket
import sys

from finecode import logger_utils
from finecode.lsp_server import global_state
from finecode.lsp_server.lsp_server import LspServer, create_lsp_server


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def start(
    comm_type: str,
    host: str | None = None,
    port: int | None = None,
    log_level: str = "INFO",
) -> None:
    global_state.lsp_log_file_path = logger_utils.init_logger(log_name="lsp_server", log_level=log_level)
    global_state.wm_log_level = log_level
    server: LspServer = create_lsp_server()
    if comm_type == "tcp":
        if port is None:
            port = _find_free_port()
            sys.stdout.write(f"port:{port}\n")
            sys.stdout.flush()
        await server.start_tcp_async(host or "127.0.0.1", port)
    else:
        await server.start_io_async()
