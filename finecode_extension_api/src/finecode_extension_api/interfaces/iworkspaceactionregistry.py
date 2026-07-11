from __future__ import annotations

import dataclasses
import typing

from finecode_extension_api import service

__all__ = ["HandlerInfo", "ActionInfo", "IWorkspaceActionRegistry"]


@dataclasses.dataclass(frozen=True)
class HandlerInfo:
    name: str
    source: str
    env: str
    file_loc: str | None


@dataclasses.dataclass(frozen=True)
class ActionInfo:
    name: str
    source: str
    canonical_source: str | None
    scope: str
    project: str
    language: str | None
    parent_action_source: str | None
    file_loc: str | None
    handlers: list[HandlerInfo]


class IWorkspaceActionRegistry(service.Service, typing.Protocol):
    """Read-only access to the workspace action and handler registry."""

    async def list_actions(self) -> list[ActionInfo]: ...
