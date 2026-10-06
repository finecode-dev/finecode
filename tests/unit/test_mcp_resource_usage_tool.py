"""The MCP resource-usage tool must expose the WM snapshot as JSON."""

from __future__ import annotations

import json

from finecode.mcp_server import server


def test_tool_listed_with_boolean_flag() -> None:
    """Clients discover the tool and its opt-in flag from the tool list."""
    tool = next(t for t in server._META_TOOLS if t["name"] == "get_resource_usage")

    assert tool["inputSchema"]["properties"]["includeProcesses"]["type"] == "boolean"


async def test_dispatch_returns_client_dict_as_json(monkeypatch) -> None:
    """The tool must pass the flag through and return the WM result."""
    seen: dict = {}
    snapshot = {"timestamp": 1.0, "wm": {"pid": 7}}

    class _FakeClient:
        async def get_resource_usage(self, include_processes: bool = False, **kwargs):
            seen["include_processes"] = include_processes
            return snapshot

    monkeypatch.setattr(server, "_wm_client", _FakeClient())

    async def _noop() -> None:
        return None

    monkeypatch.setattr(server, "_ensure_wm_connected", _noop)

    result = await server._handle_call_tool(
        {"name": "get_resource_usage", "arguments": {"includeProcesses": True}}
    )

    assert json.loads(result["content"][0]["text"]) == snapshot
    assert seen["include_processes"] is True
