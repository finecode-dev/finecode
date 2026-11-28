"""Tests for the ER-side workspace info provider's root and action queries.

The provider is the ER's only view of workspace topology. A wrong answer here
becomes a wrong precondition in the pyrefly host setup: a missing root silently
picks N per-project servers, and a wrong ``declares`` refusal blocks a valid
workspace.
"""

from __future__ import annotations

import pathlib
from typing import Any

from finecode_extension_runner.impls.workspace_info_provider import (
    WorkspaceInfoProviderImpl,
)


async def test_get_workspace_root_returns_none_when_the_wm_cannot_choose() -> None:
    """An ambiguous root must surface as None; the caller refuses, it does not
    fall back to a project directory."""

    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "workspace/getWorkspaceRoot"
        assert params == {}
        return {"root": None}

    provider = WorkspaceInfoProviderImpl(send)

    assert await provider.get_workspace_root() is None


async def test_get_workspace_root_returns_the_wm_path() -> None:
    async def send(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {"root": "/workspace"}

    provider = WorkspaceInfoProviderImpl(send)

    assert await provider.get_workspace_root() == pathlib.Path("/workspace")


async def test_project_declares_action_sends_camel_case_and_reads_declares() -> None:
    """The wire carries camelCase keys; a wrong key silently answers False and
    would refuse a root that does declare the host action."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append((method, params))
        return {"declares": True}

    provider = WorkspaceInfoProviderImpl(send)

    declares = await provider.project_declares_action(
        pathlib.Path("/workspace/root"), "pkg.HostAction"
    )

    assert declares is True
    assert seen == [
        (
            "workspace/projectDeclaresAction",
            {"path": "/workspace/root", "actionSource": "pkg.HostAction"},
        )
    ]
