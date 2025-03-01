from __future__ import annotations

import inspect
import logging
from pathlib import Path

from loguru import logger

from finecode import communication_utils, logs  # pygls_server_utils
from finecode.workspace_manager import app_dirs
from finecode.workspace_manager.server.lsp_server import create_lsp_server

# async def start(
#     comm_type: communication_utils.CommunicationType,
#     host: str | None = None,
#     port: int | None = None,
#     trace: bool = False,
# ) -> None:
#     log_dir_path = Path(app_dirs.get_app_dirs().user_log_dir)
#     logger.remove()
#     # disable logging raw messages
#     # TODO: make configurable
#     logger.configure(activation=[("pygls.protocol.json_rpc", False)])

#     logs.save_logs_to_file(
#         file_path=log_dir_path / "execution.log",
#         log_level="TRACE" if trace else "INFO",
#         stdout=False,
#     )

#     server = create_lsp_server()
#     if comm_type == communication_utils.CommunicationType.TCP:
#         if host is None or port is None:
#             raise ValueError("TCP server requires host and port to be provided.")

#         await pygls_server_utils.start_tcp_async(server, host, port)
#     elif comm_type == communication_utils.CommunicationType.WS:
#         if host is None or port is None:
#             raise ValueError("WS server requires host and port to be provided.")
#         raise NotImplementedError()  # async version of start_ws is needed
#     else:
#         # await pygls_utils.start_io_async(server)
#         server.start_io()


def start_sync(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    log_dir_path = Path(app_dirs.get_app_dirs().user_log_dir)
    logger.remove()
    # disable logging raw messages
    # TODO: make configurable
    logger.configure(activation=[("pygls.protocol.json_rpc", False)])
    logs.save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=False,
    )

    # pygls uses standard python logger, intercept it and pass logs to loguru
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Get corresponding Loguru level if it exists.
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where originated the logged message.
            frame, depth = inspect.currentframe(), 0
            while frame and (
                depth == 0 or frame.f_code.co_filename == logging.__file__
            ):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    server = create_lsp_server()
    server.start_io()
