from __future__ import annotations

from finecode.lsp_server import communication_utils, global_state
from finecode import logger_utils
from finecode.lsp_server.lsp_server import create_lsp_server


async def start(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    log_level: str = "INFO",
) -> None:
    logger_utils.init_logger(log_name="lsp_server", log_level=log_level)
    global_state.wm_log_level = log_level
    server = create_lsp_server()
    await server.start_io_async()
