"""The recovery tools an AI caller sees over MCP describe their own coverage.

FineCode never detects that a runner is stale, so a caller picks a recovery rung
from the tool description alone. A description that does not say which edits it
covers -- or does not point at the operation that covers the rest -- leaves the
caller to guess, and the usual guess is the cheapest tool, which silently does
nothing for the change that was actually made.
"""

from __future__ import annotations

import pathlib
import typing

import pytest

from finecode.mcp_server import catalog as catalog_module
from finecode.mcp_server import core as core_module
from finecode.mcp_server.core import ToolService
from finecode.mcp_server.legacy import LegacyFront

# Each recovery tool, the operation that covers what it does not, a change class
# it must claim, and whether it is the rung that picks up a configuration edit.
# Together these are what make the ladder navigable.
_RECOVERY_TOOLS = {
    "reload_action": {
        "next_rung": "restart_runner",
        "covers": "handler",
        "covers_configuration": False,
    },
    "restart_runner": {
        "next_rung": "reload_config",
        "covers": "shared librar",
        "covers_configuration": False,
    },
    "reload_config": {
        "next_rung": "restart_wm",
        "covers": "pyproject.toml",
        "covers_configuration": True,
    },
    "restart_wm": {
        "next_rung": "nothing above it",
        "covers": "workspace server",
        # The widest rung: replacing the process re-reads everything below it.
        "covers_configuration": True,
    },
}

_RECOVERY_ARGUMENTS = {
    "reload_action": {"action": "lint"},
    "restart_runner": {"project": "/p"},
    "reload_config": {"project": "/p"},
    "restart_wm": {},
}

# The WM operations each rung must reach. `reload_action` first refreshes the
# name mapping, so its lookup is part of the dispatch.
_RECOVERY_OPERATIONS = {
    "reload_action": {"actions/list", "actions/reload"},
    "restart_runner": {"runners/restart"},
    "reload_config": {"workspace/reloadConfig"},
    "restart_wm": {"server/getInfo", "replace_running_server"},
}


def _tool(name: str) -> dict:
    tool = next((t for t in catalog_module.BUILTIN_TOOLS if t["name"] == name), None)
    assert tool is not None, f"{name} is not offered as an MCP tool"
    return typing.cast(dict, tool)


def _sections(description: str) -> tuple[str, str]:
    covers, marker, rest = description.partition("Does not cover:")
    assert marker, "description must state what it does not cover"
    _, covers_marker, covered = covers.partition("Covers:")
    assert covers_marker, "description must state what it covers"
    return covered, rest


@pytest.mark.parametrize("tool_name", sorted(_RECOVERY_TOOLS))
def test_recovery_tool_names_its_coverage_and_the_next_rung(tool_name: str) -> None:
    """PRD-0008-AC11 — each recovery tool names the classes of change it covers
    and the operation to reach for when a change falls outside them."""
    expected = _RECOVERY_TOOLS[tool_name]
    covered, not_covered = _sections(_tool(tool_name)["description"])

    assert expected["covers"] in covered.lower()
    assert expected["covers"] not in not_covered.lower()

    assert expected["next_rung"] in not_covered
    assert expected["next_rung"] not in covered


@pytest.mark.parametrize("tool_name", sorted(_RECOVERY_TOOLS))
def test_configuration_coverage_is_claimed_by_exactly_the_rung_that_has_it(
    tool_name: str,
) -> None:
    """PRD-0008-AC11 — only the configuration rung says it picks up an edit to
    pyproject.toml or a preset; the code rungs say they do not.

    A runner re-reads code, not configuration, so a caller who restarts after a
    config edit gets a fresh process running the configuration it already had —
    and no error to tell them so.
    """
    covered, not_covered = _sections(_tool(tool_name)["description"])
    claims_configuration = _RECOVERY_TOOLS[tool_name]["covers_configuration"]

    assert ("configuration" in covered.lower()) is claims_configuration
    assert ("configuration" in not_covered.lower()) is not claims_configuration


def test_the_code_rungs_never_claim_configuration_coverage() -> None:
    """PRD-0008-AC11 — a caller who edited configuration is told by both cheap
    rungs that they are the wrong tool, and by name which is the right one.

    Coverage grows monotonically up the ladder, so the wide rungs claim it and
    the narrow ones must not.
    """
    claiming = {
        name
        for name in _RECOVERY_TOOLS
        if "configuration" in _sections(_tool(name)["description"])[0].lower()
    }
    assert claiming == {"reload_config", "restart_wm"}


class _RecordingWmClient:
    """Records which WM operation each recovery rung reaches."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, typing.Any]]] = []

    def on_notification(self, name: str, handler: typing.Any) -> None:
        pass

    def on_request(self, name: str, handler: typing.Any) -> None:
        pass

    def configure_reconnect(self, policy: typing.Any, on_reattach: typing.Any) -> None:
        pass

    async def connect(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def add_dir(self, *args: typing.Any, **kwargs: typing.Any) -> dict:
        return {"projects": []}

    async def list_actions(self, **kwargs: typing.Any) -> typing.Any:
        from types import SimpleNamespace

        self.calls.append(("actions/list", kwargs))
        return SimpleNamespace(actions=[], unresolved_projects=[])

    async def reload_action(
        self, action_source: str, project: str | None = None
    ) -> dict:
        self.calls.append(
            ("actions/reload", {"action_source": action_source, "project": project})
        )
        return {"reloaded": True}

    async def restart_runner(self, **kwargs: typing.Any) -> dict:
        self.calls.append(("runners/restart", kwargs))
        return {"restarted": []}

    async def reload_config(self, **kwargs: typing.Any) -> list[dict]:
        self.calls.append(("workspace/reloadConfig", kwargs))
        return [{"project": "/p", "status": "recovered"}]

    async def get_info(self) -> dict:
        self.calls.append(("server/getInfo", {}))
        return {"pid": 7, "clients": []}


class _NullSession:
    def on_request(self, method: str, handler: typing.Any) -> None:
        pass

    def on_notification(self, method: str, handler: typing.Any) -> None:
        pass

    async def send_notification(self, method: str, params: dict | None = None) -> None:
        pass


@pytest.mark.parametrize("tool_name", sorted(_RECOVERY_TOOLS))
async def test_recovery_tools_are_dispatchable(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD-0008-AC11 — a described tool that cannot be called is a description
    of nothing; each recovery rung reaches its own WM operation."""
    front = LegacyFront(typing.cast(typing.Any, _NullSession()))
    client = _RecordingWmClient()
    service = ToolService(
        workdir=pathlib.Path("/p"),
        port=1,
        client=typing.cast(typing.Any, client),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )

    async def _already_connected() -> None:
        return None

    monkeypatch.setattr(service, "ensure_connected", _already_connected)

    async def _replaced(*args: typing.Any, **kwargs: typing.Any) -> dict:
        client.calls.append(("replace_running_server", {}))
        return {"port": 9}

    monkeypatch.setattr(core_module.wm_lifecycle, "replace_running_server", _replaced)

    result = await front.handle_call_tool(
        service,
        {"name": tool_name, "arguments": dict(_RECOVERY_ARGUMENTS[tool_name])},
    )

    reached = {method for method, _ in client.calls}
    assert "isError" not in result
    assert result["content"][0]["text"]
    assert reached >= _RECOVERY_OPERATIONS[tool_name], f"{tool_name} reached {reached}"
