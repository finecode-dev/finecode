# docs: docs/cli.md
"""FineCode MCP Server — proxy to the FineCode WM server.

Connects to the FineCode WM server over TCP JSON-RPC and translates MCP tool calls into
WM server requests. If no WM server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys

from loguru import logger

import finecode_jsonrpc
from finecode.mcp_server.core import ToolService, prepare_wm
from finecode.mcp_server.legacy import LegacyFront
from finecode.wm_client import ApiClient


def start(workdir: pathlib.Path, port_file: pathlib.Path | None = None) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API.

    If *port_file* is given, a dedicated WM server is started that writes its
    port to that file instead of the shared discovery file.

    The WM connection is established lazily on the first ``tools/list`` call so
    that the MCP client name (from the ``initialize`` handshake) can be included
    in the ``client_id`` sent to the WM server.
    """
    try:
        wm_port = prepare_wm(workdir, port_file)
    except TimeoutError as exc:
        logger.error(str(exc))
        sys.exit(1)

    async def _run() -> None:
        transport = finecode_jsonrpc.ServerStdioTransport(
            readable_id="mcp_server", framing="newline"
        )
        session = finecode_jsonrpc.JsonRpcServerSession()
        session.attach(transport)
        front = LegacyFront(session)
        service = ToolService(
            workdir=workdir,
            port=wm_port,
            client=ApiClient(),
            dependencies=front.dependencies(),
            effects=front.effects(),
        )
        front.register(service)

        await transport.start()
        logger.info("MCP: stdio server ready")
        while not transport._stop_event.is_set():
            await asyncio.sleep(0.05)

        logger.info("MCP: stdio transport stopped")
        if service.is_connected:
            logger.info("MCP: Closing WM client")
            await service.close()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
