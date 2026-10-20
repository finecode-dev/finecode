"""The MCP `dump_config` tool forwards an optional `format` flag as `format_output`.

The only formatted `finecode_config_dump/` writes come from a user-requested dump, and
the MCP tool is one of the ways a user asks: the tool must map `format: false` to
`format_output: false` on the action payload so a formatter that cannot run (or an agent
that wants the raw dump) can be honoured, and the default path must leave the payload
unchanged — absent means "format, please".
"""

from __future__ import annotations

import pytest

from finecode.mcp_server import server


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


@pytest.fixture
def mcp_session(monkeypatch: pytest.MonkeyPatch) -> _StubWmClient:
    wm_client = _StubWmClient()
    monkeypatch.setattr(server, "_wm_client", wm_client)
    monkeypatch.setattr(server, "_wm_connected", True)
    monkeypatch.setattr(server, "_session", None)
    return wm_client


async def test_format_false_maps_to_format_output_false(mcp_session) -> None:
    """`format: false` reaches the action as `format_output: false`, so the
    dump is written as rendered with no formatter dispatch."""
    await server._handle_call_tool(
        {"name": "dump_config", "arguments": {"project": "/p", "format": False}}
    )

    assert len(mcp_session.run_actions) == 1
    assert mcp_session.run_actions[0]["params"]["format_output"] is False


async def test_default_omits_format_output_from_the_payload(mcp_session) -> None:
    """Without `format`, the payload carries no `format_output` key — the
    action's own default (format) applies, and the default path sends an
    unchanged payload."""
    await server._handle_call_tool(
        {"name": "dump_config", "arguments": {"project": "/p"}}
    )

    assert len(mcp_session.run_actions) == 1
    assert "format_output" not in mcp_session.run_actions[0]["params"]


async def test_format_true_omits_format_output_from_the_payload(mcp_session) -> None:
    """`format: true` is the explicit spelling of the default: still no
    `format_output` key on the payload."""
    await server._handle_call_tool(
        {"name": "dump_config", "arguments": {"project": "/p", "format": True}}
    )

    assert len(mcp_session.run_actions) == 1
    assert "format_output" not in mcp_session.run_actions[0]["params"]
