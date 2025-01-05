from __future__ import annotations

from pathlib import Path

from loguru import logger
from modapp.extras.logs import save_logs_to_file
from modapp.extras.platformdirs import get_dirs

import finecode.communication_utils as communication_utils
import finecode.pygls_server_utils as pygls_server_utils
from finecode.workspace_manager.server.lsp_server import create_lsp_server


async def start(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    log_dir_path = Path(
        get_dirs(
            app_name="FineCode_Workspace_Manager", app_author="FineCode", version="1.0"
        ).user_log_dir
    )
    logger.remove()
    save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=False,
    )

    server = create_lsp_server()
    if comm_type == communication_utils.CommunicationType.TCP:
        if host is None or port is None:
            raise ValueError("TCP server requires host and port to be provided.")

        await pygls_server_utils.start_tcp_async(server, host, port)
    elif comm_type == communication_utils.CommunicationType.WS:
        if host is None or port is None:
            raise ValueError("WS server requires host and port to be provided.")
        raise NotImplementedError()  # async version of start_ws is needed
    else:
        # await pygls_utils.start_io_async(server)
        server.start_io()


def start_sync(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    log_dir_path = Path(
        get_dirs(
            app_name="FineCode_Workspace_Manager", app_author="FineCode", version="1.0"
        ).user_log_dir
    )
    logger.remove()
    save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=False,
    )

    server = create_lsp_server()
    server.start_io()


# async def start_in_ws_context(ws_context: context.WorkspaceContext) -> None:
#     # one for all, doesn't need to change on ws dirs change
#     asyncio.create_task(handle_runners_lifecycle(ws_context))
