"""Slot through which the runner asks the editor to apply a workspace edit.

An ER sends ``workspace/applyEdit`` to the runner's JSON-RPC client, meaning
"have the editor perform this edit". Actually sending that request to the IDE
is owned by the layer that holds the editor connection, which sits above the
runner in the WM's layer stack. See ADR-0072 for why this is a slot the owner
fills on import rather than an upward import.

Unlike ``wm_bridge``, an unfilled slot here is **not** droppable: the caller is
waiting on a result only the editor can produce, so ``handlers()`` returns
``None`` and the call site answers the ER with an error, exactly as
``elicitation_bridge`` and ``run_dispatch_bridge`` do.
"""

from __future__ import annotations

import enum
import typing

from finecode.wm_server.runner import _internal_client_types

__all__ = [
    "ApplyWorkspaceEditBridge",
    "ResourceOperationKind",
    "handlers",
    "install",
    "reset",
]


class ResourceOperationKind(enum.StrEnum):
    """A resource operation an editor may declare it can perform.

    The three kinds LSP defines under ``workspace.workspaceEdit.resourceOperations``.
    A `StrEnum` because the values are the protocol's own wire strings, and the
    same strings tag the operations inside a ``WorkspaceEdit``.
    """

    CREATE = "create"
    RENAME = "rename"
    DELETE = "delete"


class ApplyWorkspaceEditBridge(typing.Protocol):
    """What the runner needs from whoever owns the editor connection."""

    def supported_resource_operations(self) -> frozenset[ResourceOperationKind]:
        """The resource operations the editor can perform, or empty when it
        declared none (the LSP default when the client is silent)."""

    async def apply_workspace_edit(
        self, params: _internal_client_types.ApplyWorkspaceEditParams
    ) -> _internal_client_types.ApplyWorkspaceEditResult:
        """Ask the editor to apply *params* and wait for its answer."""


_installed: ApplyWorkspaceEditBridge | None = None


def install(implementation: ApplyWorkspaceEditBridge) -> None:
    """Nominate *implementation* as the answer to apply-edit requests from an ER."""
    global _installed
    _installed = implementation


def reset() -> None:
    """Forget the installed implementation. Tests only."""
    global _installed
    _installed = None


def handlers() -> ApplyWorkspaceEditBridge | None:
    """The installed implementation, or ``None`` if nobody filled the slot.

    ``None`` is a real state rather than a defect: a WM assembled without its
    editor connection has nobody to ask, and the caller is waiting on a result.
    """
    return _installed
