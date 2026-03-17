# docs: docs/cli.md
"""FineCode MCP Server — proxy to the FineCode WM server.

Connects to the FineCode WM server over TCP JSON-RPC and translates MCP tool calls into
WM server requests. If no WM server is running, starts one as a subprocess.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from contextlib import asynccontextmanager

from loguru import logger
from fastmcp import FastMCP

from finecode.wm_server import wm_lifecycle
from finecode.wm_client import ApiClient


_wm_client = ApiClient()


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
                project: str,  # absolute path to the project directory (e.g. /home/user/myrepo)
                file_paths: list[str] | None = None,
            ) -> dict:
                return await _wm_client.run_action(
                    action_name,
                    project,
                    params={"file_paths": file_paths} if file_paths else None,
                    options={
                        "resultFormats": ["json", "string"],
                        "trigger": "user",
                        "devEnv": "ai",
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
            await _wm_client.connect("127.0.0.1", port)
        except (ConnectionRefusedError, OSError) as exc:
            logger.error(f"Could not connect to FineCode WM server on port {port}: {exc}")
            sys.exit(1)
        logger.debug(f"Add dir to API Client: {workdir}")
        await _wm_client.add_dir(workdir)
        logger.debug("Added dir")
        actions = await _wm_client.list_actions()
        logger.info(f"Registering {len(actions)} action tools")
        _register_action_tools(server, actions)
        try:
            yield
        finally:
            await _wm_client.close()
            # The WM server will auto-stop after the last client disconnects.

    mcp = FastMCP("FineCode", lifespan=lifespan)

    @mcp.tool(
        name="list_projects",
        description="List all projects in the FineCode workspace with their names, paths, and statuses",
    )
    async def list_projects() -> dict:
        result = await _wm_client.list_projects()
        return {"projects": result}

    return mcp


def start(workdir: pathlib.Path, port_file: pathlib.Path | None = None) -> None:
    """Start the MCP server on stdio, connecting to the FineCode API.

    If *port_file* is given, a dedicated WM server is started that writes its
    port to that file instead of the shared discovery file.
    """
    if port_file is not None:
        wm_lifecycle.start_own_server(workdir, port_file=port_file)
        try:
            port = asyncio.run(wm_lifecycle.wait_until_ready_from_file(port_file))
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)
    else:
        wm_lifecycle.ensure_running(workdir)
        try:
            port = asyncio.run(wm_lifecycle.wait_until_ready())
        except TimeoutError as exc:
            logger.error(str(exc))
            sys.exit(1)

    mcp = create_mcp_server(workdir, port)
    mcp.run()
