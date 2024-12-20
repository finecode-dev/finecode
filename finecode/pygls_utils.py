import logging
import sys
from threading import Event
import asyncio
from typing import BinaryIO, Optional

from loguru import logger
from pygls.lsp.server import LanguageServer
from pygls.io_ import run_async, StdinAsyncReader, StdoutWriter


std_logger = logging.getLogger(__name__)


async def start_tcp_async(server: LanguageServer, host: str, port: int) -> None:
    """Starts TCP server."""
    logger.info(f"Starting TCP server on {host}:{port}")

    server._stop_event = stop_event = Event()

    async def lsp_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        logger.debug("Connected to client")
        self.protocol.set_writer(writer)  # type: ignore
        await run_async(
            stop_event=stop_event,
            reader=reader,
            protocol=server.protocol,
            logger=std_logger,
            error_handler=server.report_server_error,
        )
        logger.debug("Main loop finished")
        server.shutdown()

    async def tcp_server(h: str, p: int):
        server._server = await asyncio.start_server(lsp_connection, h, p)

        addrs = ", ".join(str(sock.getsockname()) for sock in server._server.sockets)
        logger.info(f"Serving on {addrs}")

        async with server._server:
            await server._server.serve_forever()

    try:
        await tcp_server(host, port)
    except asyncio.CancelledError:
        logger.debug("Server was cancelled")


async def start_io_async(
    server: LanguageServer, stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None
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
