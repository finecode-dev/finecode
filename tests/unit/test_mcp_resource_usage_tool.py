"""The MCP resource-usage tool must expose the WM snapshot as JSON."""

from __future__ import annotations

import json
import pathlib
import typing

from finecode.mcp_server import catalog
from finecode.mcp_server.core import ToolService
from finecode.mcp_server.legacy import LegacyFront


def test_tool_listed_with_boolean_flag() -> None:
    """Clients discover the tool and its opt-in flag from the tool list."""
    tool = next(t for t in catalog.BUILTIN_TOOLS if t["name"] == "get_resource_usage")

    flag = typing.cast(
        dict[str, typing.Any],
        tool["inputSchema"]["properties"]["includeProcesses"],
    )

    assert flag["type"] == "boolean"


class _NullSession:
    def on_request(self, method: str, handler: typing.Any) -> None:
        pass

    def on_notification(self, method: str, handler: typing.Any) -> None:
        pass


async def test_dispatch_returns_client_dict_as_json(monkeypatch) -> None:
    """The tool must pass the flag through and return the WM result."""
    seen: dict = {}
    snapshot = {"timestamp": 1.0, "wm": {"pid": 7}}

    class _FakeClient:
        async def get_resource_usage(
            self, include_processes: bool = False, **kwargs: typing.Any
        ) -> typing.Any:
            seen["include_processes"] = include_processes
            return snapshot

    front = LegacyFront(typing.cast(typing.Any, _NullSession()))
    service = ToolService(
        workdir=pathlib.Path("/p"),
        port=1,
        client=typing.cast(typing.Any, _FakeClient()),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )

    async def _already_connected() -> None:
        return None

    monkeypatch.setattr(service, "ensure_connected", _already_connected)

    result = await front.handle_call_tool(
        service, {"name": "get_resource_usage", "arguments": {"includeProcesses": True}}
    )

    assert json.loads(result["content"][0]["text"]) == snapshot
    assert seen["include_processes"] is True
