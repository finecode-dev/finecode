"""The recovery tools an AI caller sees over MCP describe their own coverage.

FineCode never detects that a runner is stale, so a caller picks a recovery rung
from the tool description alone. A description that does not say which edits it
covers -- or does not point at the operation that covers the rest -- leaves the
caller to guess, and the usual guess is the cheapest tool, which silently does
nothing for the change that was actually made.
"""

from __future__ import annotations

import inspect

import pytest

from finecode.mcp_server import server

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


def _tool(name: str) -> dict:
    tool = next((t for t in server._META_TOOLS if t["name"] == name), None)
    assert tool is not None, f"{name} is not offered as an MCP tool"
    return tool


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


def test_recovery_tools_are_dispatchable() -> None:
    """PRD-0008-AC11 — a described tool that cannot be called is a description
    of nothing; every recovery tool is also handled by the call dispatcher."""

    dispatch_source = inspect.getsource(server._handle_call_tool)
    for tool_name in _RECOVERY_TOOLS:
        assert f'name == "{tool_name}"' in dispatch_source
