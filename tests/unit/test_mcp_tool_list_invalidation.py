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
import pathlib
import typing

import pytest

from finecode.mcp_server.core import ToolService
from finecode.mcp_server.legacy import LegacyFront, LegacyToolResult


class _RecordingSession:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict]] = []

    def on_request(self, method: str, handler: typing.Any) -> None:
        pass

    def on_notification(self, method: str, handler: typing.Any) -> None:
        pass

    async def send_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))


class _StubWmClient:
    def __init__(self, projects: list[dict]) -> None:
        self._projects = projects
        self.calls: list[dict] = []

    def on_notification(self, name: str, handler: typing.Any) -> None:
        pass

    def on_request(self, name: str, handler: typing.Any) -> None:
        pass

    def configure_reconnect(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        pass

    async def connect(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def add_dir(self, *args: typing.Any, **kwargs: typing.Any) -> dict:
        return {"projects": []}

    async def reload_config(
        self,
        project=None,
        *,
        all_projects=False,
        rescan=False,
        kill_in_flight_runs=False,
    ) -> list[dict]:
        self.calls.append(
            {
                "project": project,
                "all_projects": all_projects,
                "rescan": rescan,
                "kill_in_flight_runs": kill_in_flight_runs,
            }
        )
        return self._projects


@pytest.fixture
def mcp_pair() -> tuple[LegacyFront, ToolService, _RecordingSession]:
    session = _RecordingSession()
    front = LegacyFront(typing.cast(typing.Any, session))
    service = ToolService(
        workdir=pathlib.Path("/p"),
        port=1,
        client=typing.cast(
            typing.Any,
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
        ),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )
    service._tool_name_to_source = {"lint": "pkg.LintAction"}
    return front, service, session


async def _call_reload_config(
    front: LegacyFront, service: ToolService, arguments: dict
) -> LegacyToolResult:
    return await front.handle_call_tool(
        service, {"name": "reload_config", "arguments": arguments}
    )


async def test_action_set_change_invalidates_the_client_tool_list(
    mcp_pair,
) -> None:
    """A recovery that added or removed an action clears the cached tool
    mapping and notifies the client."""
    front, service, session = mcp_pair
    service._client = typing.cast(
        typing.Any,
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

    await _call_reload_config(front, service, {"project": "/p"})

    assert service._tool_name_to_source == {}
    assert session.notifications == [("notifications/tools/list_changed", {})]


async def test_unchanged_action_set_does_not_notify(mcp_pair) -> None:
    """A recovery that changed only handler configuration leaves the tool list
    alone — a client that re-fetches on every notification would otherwise pay
    for every recovery."""
    front, service, session = mcp_pair

    await _call_reload_config(front, service, {"project": "/p"})

    assert service._tool_name_to_source == {"lint": "pkg.LintAction"}
    assert session.notifications == []


def test_server_advertises_list_changed_capability() -> None:
    """The notification is only permitted if the capability was advertised at
    initialize time, so a client that never saw it may ignore it."""
    front = LegacyFront(typing.cast(typing.Any, _RecordingSession()))

    capabilities = asyncio.run(front.handle_initialize({}))["capabilities"]

    assert capabilities["tools"]["listChanged"] is True
