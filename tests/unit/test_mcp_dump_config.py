"""The MCP `dump_config` tool forwards an optional `format` flag as `format_output`.

The only formatted `finecode_config_dump/` writes come from a user-requested dump, and
the MCP tool is one of the ways a user asks: the tool must map `format: false` to
`format_output: false` on the action payload so a formatter that cannot run (or an agent
that wants the raw dump) can be honoured, and the default path must leave the payload
unchanged — absent means "format, please".
"""

from __future__ import annotations

import pathlib
import typing

import pytest

from finecode.mcp_server.core import ToolService
from finecode.mcp_server.legacy import LegacyFront


class _StubWmClient:
    def __init__(self) -> None:
        self.run_actions: list[dict] = []

    async def get_project_raw_config(self, _project: str) -> dict:
        return {"tool": {}}

    async def run_action(
        self, action_source: str, project: str, params: dict, options: dict
    ) -> dict:
        self.run_actions.append(
            {
                "action_source": action_source,
                "project": project,
                "params": params,
                "options": options,
            }
        )
        return {"returnCode": 0}


class _NullSession:
    def on_request(self, method: str, handler: typing.Any) -> None:
        pass

    def on_notification(self, method: str, handler: typing.Any) -> None:
        pass


@pytest.fixture
def mcp_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[LegacyFront, ToolService, _StubWmClient]:
    wm_client = _StubWmClient()
    front = LegacyFront(typing.cast(typing.Any, _NullSession()))
    service = ToolService(
        workdir=pathlib.Path("/p"),
        port=1,
        client=typing.cast(typing.Any, wm_client),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )

    async def _already_connected() -> None:
        return None

    monkeypatch.setattr(service, "ensure_connected", _already_connected)
    return front, service, wm_client


async def test_format_false_maps_to_format_output_false(mcp_pair) -> None:
    """`format: false` reaches the action as `format_output: false`, so the
    dump is written as rendered with no formatter dispatch."""
    front, service, wm_client = mcp_pair
    await front.handle_call_tool(
        service,
        {"name": "dump_config", "arguments": {"project": "/p", "format": False}},
    )

    assert len(wm_client.run_actions) == 1
    assert wm_client.run_actions[0]["params"]["format_output"] is False


async def test_default_omits_format_output_from_the_payload(mcp_pair) -> None:
    """Without `format`, the payload carries no `format_output` key — the
    action's own default (format) applies, and the default path sends an
    unchanged payload."""
    front, service, wm_client = mcp_pair
    await front.handle_call_tool(
        service, {"name": "dump_config", "arguments": {"project": "/p"}}
    )

    assert len(wm_client.run_actions) == 1
    assert "format_output" not in wm_client.run_actions[0]["params"]


async def test_format_true_omits_format_output_from_the_payload(mcp_pair) -> None:
    """`format: true` is the explicit spelling of the default: still no
    `format_output` key on the payload."""
    front, service, wm_client = mcp_pair
    await front.handle_call_tool(
        service, {"name": "dump_config", "arguments": {"project": "/p", "format": True}}
    )

    assert len(wm_client.run_actions) == 1
    assert "format_output" not in wm_client.run_actions[0]["params"]
