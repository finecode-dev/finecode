from __future__ import annotations

from finecode import communication_utils  # pygls_server_utils
from finecode.workspace_manager import logger_utils
from finecode.workspace_manager.lsp_server.lsp_server import create_lsp_server

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
    logger_utils.init_logger(trace=trace)
    server = create_lsp_server()
    server.start_io()
