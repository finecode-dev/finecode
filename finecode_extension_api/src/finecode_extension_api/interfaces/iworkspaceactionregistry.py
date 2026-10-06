from __future__ import annotations

import dataclasses
import typing

from finecode_extension_api import service

__all__ = ["ActionInfo", "HandlerInfo", "IWorkspaceActionRegistry"]


@dataclasses.dataclass(frozen=True)
class HandlerInfo:
    """A handler as the workspace registry knows it.

    ``source`` is the config-facing alias (as written in the definition file,
    usually a package-level re-export); ``canonical_source`` is the module the
    class is actually defined in. Key handlers by ``canonical_source`` when it
    is available — two different aliases may name the same real handler.
    ``canonical_source`` is ``None`` until the env's runner has started, or
    permanently if the class cannot be imported there.
    """

    name: str
    source: str
    canonical_source: str | None
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
