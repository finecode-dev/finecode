import logging
import sys
from threading import Event
from typing import Any, BinaryIO, Optional

from loguru import logger
from pygls.io_ import StdinAsyncReader, StdoutWriter, run_async
from pygls.lsp.server import LanguageServer

std_logger = logging.getLogger(__name__)


# async def start_tcp_async(server: LanguageServer, host: str, port: int) -> None:
#     """Starts TCP server."""
#     logger.info(f"Starting TCP server on {host}:{port}")

#     server._stop_event = stop_event = Event()

#     async def lsp_connection(
#         reader: asyncio.StreamReader, writer: asyncio.StreamWriter
#     ):
#         logger.debug("Connected to client")
#         self.protocol.set_writer(writer)  # type: ignore
#         await run_async(
#             stop_event=stop_event,
#             reader=reader,
#             protocol=server.protocol,
#             logger=std_logger,
#             error_handler=server.report_server_error,
#         )
#         logger.debug("Main loop finished")
#         server.shutdown()

#     async def tcp_server(h: str, p: int):
#         server._server = await asyncio.start_server(lsp_connection, h, p)

#         addrs = ", ".join(str(sock.getsockname()) for sock in server._server.sockets)
#         logger.info(f"Serving on {addrs}")

#         async with server._server:
#             await server._server.serve_forever()

#     try:
#         await tcp_server(host, port)
#     except asyncio.CancelledError:
#         logger.debug("Server was cancelled")


async def start_io_async(
    server: LanguageServer,
    stdin: Optional[BinaryIO] = None,
    stdout: Optional[BinaryIO] = None,
):
    """Starts an asynchronous IO server."""
    logger.info("Starting async IO server")

    server._stop_event = Event()
    reader = StdinAsyncReader(stdin or sys.stdin.buffer, server.thread_pool)
    writer = StdoutWriter(stdout or sys.stdout.buffer)
    server.protocol.set_writer(writer)

    try:
        await run_async(
            stop_event=server._stop_event,
            reader=reader,
            protocol=server.protocol,
            logger=std_logger,
            error_handler=server.report_server_error,
        )
    except BrokenPipeError:
        logger.error("Connection to the client is lost! Shutting down the server.")
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.shutdown()


def deserialize_pygls_object(pygls_object) -> dict[str, Any] | list[Any]:
    deserialized: dict[str, Any] | list[Any]
    if "_0" in pygls_object._fields:
        # list
        deserialized = []
        for index in range(len(pygls_object)):
            item = getattr(pygls_object, f"_{index}")
            if hasattr(item, "__module__") and item.__module__ == "pygls.protocol":
                deserialized_value = deserialize_pygls_object(item)
            else:
                deserialized_value = item
            deserialized.append(deserialized_value)
    else:
        # dict
        deserialized = {}
        for field_name in pygls_object._fields:
            field_value = getattr(pygls_object, field_name)
            if (
                hasattr(field_value, "__module__")
                and field_value.__module__ == "pygls.protocol"
            ):
                deserialized_value = deserialize_pygls_object(field_value)
            else:
                deserialized_value = field_value
            deserialized[field_name] = deserialized_value
    return deserialized
