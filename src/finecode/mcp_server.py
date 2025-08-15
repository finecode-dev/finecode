import asyncio
import pathlib
from mcp.server.fastmcp import FastMCP

from finecode import context


async def create_and_start_mcp_server(port: int, ws_context: context.WorkspaceContext) -> asyncio.Task:
    # TODO: use Server instead of FastMCP to be able to call tools dynamically
    # example: https://github.com/modelcontextprotocol/servers/blob/main/src/git/src/mcp_server_git/server.py
    mcp = FastMCP("FineCode", port=8776, json_response=True)
    
    def list_actions():
        return {
            "actions": [
                { "name": "Lint" },
                { "name": "Format" }
            ]
        }

    mcp.add_tool(fn=list_actions, name="list_actions", description="List actions available for developer in development workspace", annotations=None)
    
    def lint():
        print("perform linting...")

    mcp.add_tool(fn=lint, name="lint", description="Lint either the whole workspace or single project or even file", annotations=None)
    # TODO: ~~projects as resource?~~
    # TODO: finecode actions as mcp tools?
    
    mcp.call_tool
    # mcp.run()
    server_task = asyncio.create_task(mcp.run_streamable_http_async())
    return server_task


async def start():
    ws_context = context.WorkspaceContext([pathlib.Path('/home/user/Development/FineCode/finecode')])
    await create_and_start_mcp_server(8776, ws_context)
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(start())
