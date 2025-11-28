from __future__ import annotations

from finecode import communication_utils
from finecode import logger_utils
from finecode.lsp_server.lsp_server import create_lsp_server


async def start(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    logger_utils.init_logger(trace=trace)
    server = create_lsp_server()
    await server.start_io_async()
