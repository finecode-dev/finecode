"""FineCode MCP Server — stdio proxy to the FineCode API server.

Started by Claude Code (or other MCP clients) via .mcp.json. Connects to the
FineCode API server over TCP JSON-RPC and translates MCP tool calls into API
requests. If no API server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from contextlib import asynccontextmanager

from loguru import logger
from fastmcp import FastMCP

from finecode.api_server import api_server
from finecode.api_client import ApiClient


_api_client = ApiClient()


def _register_action_tools(mcp: FastMCP, actions: list[dict]) -> None:
    """Register one MCP tool per unique action name."""
    seen: set[str] = set()
    for action in actions:
        name = action["name"]
        if name in seen:
            continue
        seen.add(name)

        def _make_handler(action_name: str):
            async def handler(
                project: str,
                file_paths: list[str] | None = None,
            ) -> dict:
                return await _api_client.run_action(
                    action_name,
                    project,
                    params={"file_paths": file_paths} if file_paths else None,
                    options={
                        "result_formats": ["json", "string"],
                        "trigger": "user",
                        "dev_env": "ai",
                    }
                )
            handler.__name__ = action_name
            return handler

        mcp.add_tool(
            mcp.tool(name_or_fn=_make_handler(name), name=name) # title='', description=''
        )


def create_mcp_server(workdir: pathlib.Path, port: int) -> FastMCP:
    @asynccontextmanager
    async def lifespan(server):
        try:
            await _api_client.connect("127.0.0.1", port)
        except (ConnectionRefusedError, OSError) as exc:
            logger.error(f"Could not connect to FineCode API server on port {port}: {exc}")
            sys.exit(1)
        logger.debug(f"Add dir to API Client: {workdir}")
        await _api_client.add_dir(workdir)
        logger.debug("Added dir")
        actions = await _api_client.list_actions()
        logger.info(f"Registering {len(actions)} action tools")
        _register_action_tools(server, actions)
        try:
            yield
        finally:
            await _api_client.close()
            # The API server will auto-stop after the last client disconnects.

    mcp = FastMCP("FineCode", lifespan=lifespan)

    @mcp.tool(
        name="list_projects",
        description="List all projects in the FineCode workspace with their names, paths, and statuses",
    )
    async def list_projects() -> dict:
        result = await _api_client.list_projects()
        return {"projects": result}

    return mcp


def start(workdir: pathlib.Path) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API."""
    if not api_server.is_running():
        logger.info("No running FineCode API server found, starting one...")
        api_server.ensure_running(workdir)
        try:
            port = asyncio.run(api_server.wait_until_ready())
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    else:
        port = api_server.read_port()

    mcp = create_mcp_server(workdir, port)
    mcp.run()
