from __future__ import annotations

from typing import Any, Awaitable, Callable

from finecode_extension_api.interfaces.iworkspaceactionregistry import (
    ActionInfo,
    HandlerInfo,
    IWorkspaceActionRegistry,
)

__all__ = ["HandlerInfo", "ActionInfo", "IWorkspaceActionRegistry", "parse_workspace_actions"]


def parse_workspace_actions(payload: dict[str, Any]) -> list[ActionInfo]:
    """Map a ``finecode/listWorkspaceActions`` result into ``ActionInfo`` objects."""
    actions: list[ActionInfo] = []
    for raw_action in payload.get("actions", []):
        handlers = [
            HandlerInfo(
                name=raw_handler["name"],
                source=raw_handler["source"],
                env=raw_handler["env"],
                file_loc=raw_handler.get("fileLoc"),
            )
            for raw_handler in raw_action.get("handlers", [])
        ]
        actions.append(
            ActionInfo(
                name=raw_action["name"],
                source=raw_action["source"],
                canonical_source=raw_action.get("canonicalSource"),
                scope=raw_action["scope"],
                project=raw_action["project"],
                language=raw_action.get("language"),
                parent_action_source=raw_action.get("parentActionSource"),
                file_loc=raw_action.get("fileLoc"),
                handlers=handlers,
            )
        )
    return actions


class WorkspaceActionRegistryImpl(IWorkspaceActionRegistry):
    """Calls the WM back-channel finecode/listWorkspaceActions."""

    def __init__(self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]) -> None:
        self._send = send_request_to_wm

    async def list_actions(self) -> list[ActionInfo]:
        raw = await self._send("finecode/listWorkspaceActions", {})
        return parse_workspace_actions(raw)
