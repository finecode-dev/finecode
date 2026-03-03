"""FineCode MCP Server — stdio proxy to the FineCode API server.

Started by Claude Code (or other MCP clients) via .mcp.json. Connects to the
FineCode API server over TCP JSON-RPC and translates MCP tool calls into API
requests. If no API server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import pathlib
import signal
import sys

from loguru import logger
from mcp.server.fastmcp import FastMCP

from finecode.api_server import api_server
from finecode.api_client import ApiClient


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

_api_client = ApiClient()


def create_mcp_server(workdir: pathlib.Path) -> FastMCP:
    mcp = FastMCP("FineCode", json_response=True)

    @mcp.tool(
        name="list_projects",
        description="List all projects in the FineCode workspace with their names, paths, and statuses",
    )
    async def list_projects() -> dict:
        result = await _api_client.list_projects()
        return {"projects": result}

    return mcp


async def start(workdir: pathlib.Path) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API."""
    if not api_server.is_running():
        logger.info("No running FineCode API server found, starting one...")
        api_server.ensure_running(workdir)
        try:
            port = await api_server.wait_until_ready()
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    else:
        port = api_server.read_port()

    try:
        await _api_client.connect("127.0.0.1", port)
    except (ConnectionRefusedError, OSError) as exc:
        logger.error(f"Could not connect to FineCode API server on port {port}: {exc}")
        sys.exit(1)

    mcp = create_mcp_server(workdir)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _stop() -> None:
        # Cancel the asyncio task cleanly (no KeyboardInterrupt in threads).
        if main_task and not main_task.done():
            main_task.cancel()
        # Close stdin so anyio's thread-based stdin reader unblocks immediately.
        try:
            sys.stdin.buffer.close()
        except Exception:
            pass

    loop.add_signal_handler(signal.SIGINT, _stop)
    loop.add_signal_handler(signal.SIGTERM, _stop)

    try:
        await mcp.run_stdio_async()
    except asyncio.CancelledError:
        pass
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        await _api_client.close()
        # The API server will auto-stop after the last client disconnects.
