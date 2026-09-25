"""An MCP client is told when a recovery changed which actions exist.

Clients cache the tool list, and nothing prompts them to re-fetch it. An action a
configuration recovery introduced is therefore unreachable — and one it removed is
still offered — until the client is told the list changed.

This covers the emission only. That a newly added action is actually *callable*
afterwards, without the client reconnecting, is PRD-0008-AC4 and needs a real MCP
session driving a real extension runner; it lives in ``tests/e2e/``.
"""

from __future__ import annotations

import asyncio

import pytest

from finecode.mcp_server import server


class _RecordingSession:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict]] = []

    async def send_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))


class _StubWmClient:
    def __init__(self, projects: list[dict]) -> None:
        self._projects = projects
        self.calls: list[dict] = []

    async def reload_config(
        self,
        project=None,
        *,
        all_projects=False,
        rescan=False,
        kill_in_flight_runs=False,
    ):
        self.calls.append(
            {"project": project, "all_projects": all_projects, "rescan": rescan}
        )
        return self._projects


@pytest.fixture
def mcp_session(monkeypatch: pytest.MonkeyPatch) -> _RecordingSession:
    session = _RecordingSession()
    monkeypatch.setattr(server, "_session", session)
    monkeypatch.setattr(server, "_wm_connected", True)
    monkeypatch.setattr(server, "_tool_name_to_source", {"lint": "pkg.LintAction"})
    return session


async def _call_reload_config(arguments: dict) -> dict:
    return await server._handle_call_tool(
        {"name": "reload_config", "arguments": arguments}
    )


async def test_action_set_change_invalidates_the_client_tool_list(
    mcp_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery that added or removed an action clears the cached tool
    mapping and notifies the client."""
    monkeypatch.setattr(
        server,
        "_wm_client",
        _StubWmClient(
            [
                {
                    "project": "/p",
                    "status": "recovered",
                    "actionsAdded": ["typecheck"],
                    "actionsRemoved": [],
                }
            ]
        ),
    )

    await _call_reload_config({"project": "/p"})

    assert server._tool_name_to_source == {}
    assert mcp_session.notifications == [("notifications/tools/list_changed", {})]


async def test_unchanged_action_set_does_not_notify(
    mcp_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery that changed only handler configuration leaves the tool list
    alone — a client that re-fetches on every notification would otherwise pay
    for every recovery."""
    monkeypatch.setattr(
        server,
        "_wm_client",
        _StubWmClient(
            [
                {
                    "project": "/p",
                    "status": "recovered",
                    "actionsAdded": [],
                    "actionsRemoved": [],
                }
            ]
        ),
    )

    await _call_reload_config({"project": "/p"})

    assert server._tool_name_to_source == {"lint": "pkg.LintAction"}
    assert mcp_session.notifications == []


def test_server_advertises_list_changed_capability() -> None:
    """The notification is only permitted if the capability was advertised at
    initialize time, so a client that never saw it may ignore it."""

    capabilities = asyncio.run(server._handle_initialize({}))["capabilities"]

    assert capabilities["tools"]["listChanged"] is True
