"""The WM's forwarding of an ER's ``workspace/applyEdit`` to the editor.

The slot used to be a module global that was never assigned, so the first ER
to ask for an editor edit got ``TypeError: 'NoneType' object is not callable``.
These tests drive the installed-slot path directly and pin the mapping that
must preserve the ordered ``documentChanges`` array.
"""

from __future__ import annotations

import asyncio

import pytest

from finecode.wm_server import errors
from finecode.wm_server.runner import (
    _internal_client_types,
    apply_workspace_edit_bridge,
)
from finecode.wm_server.runner.apply_workspace_edit_bridge import (
    ResourceOperationKind,
)
from finecode.wm_server.runner.runner_manager import _apply_workspace_edit


class _FakeBridge:
    def __init__(self, supported: frozenset[ResourceOperationKind]) -> None:
        self._supported = supported
        self.calls: list[_internal_client_types.ApplyWorkspaceEditParams] = []

    def supported_resource_operations(self) -> frozenset[ResourceOperationKind]:
        return self._supported

    async def apply_workspace_edit(
        self, params: _internal_client_types.ApplyWorkspaceEditParams
    ) -> _internal_client_types.ApplyWorkspaceEditResult:
        self.calls.append(params)
        return _internal_client_types.ApplyWorkspaceEditResult(applied=True)


def _text_document_edit(uri: str) -> _internal_client_types.TextDocumentEdit:
    return _internal_client_types.TextDocumentEdit(
        text_document=_internal_client_types.OptionalVersionedTextDocumentIdentifier(
            uri=uri
        ),
        edits=[
            _internal_client_types.TextEdit(
                range=_internal_client_types.Range(
                    start=_internal_client_types.Position(line=0, character=0),
                    end=_internal_client_types.Position(line=0, character=0),
                ),
                new_text="x",
            )
        ],
    )


def _mixed_params() -> _internal_client_types.ApplyWorkspaceEditParams:
    return _internal_client_types.ApplyWorkspaceEditParams(
        edit=_internal_client_types.WorkspaceEdit(
            document_changes=[
                _text_document_edit("file:///a.py"),
                _internal_client_types.CreateFile(uri="file:///b.py"),
                _internal_client_types.RenameFile(
                    old_uri="file:///c.py", new_uri="file:///d.py"
                ),
                _internal_client_types.DeleteFile(uri="file:///e.py"),
            ]
        )
    )


def test_mixed_document_changes_round_trip_in_order() -> None:
    """The ordered array must reach the editor in the order it arrived --
    documentChanges ordering is part of the edit's meaning."""
    bridge = _FakeBridge(supported=frozenset(ResourceOperationKind))
    apply_workspace_edit_bridge.install(bridge)
    try:
        asyncio.run(_apply_workspace_edit(_mixed_params()))
    finally:
        apply_workspace_edit_bridge.reset()

    changes = bridge.calls[0].edit.document_changes
    assert changes is not None
    assert [type(change) for change in changes] == [
        _internal_client_types.TextDocumentEdit,
        _internal_client_types.CreateFile,
        _internal_client_types.RenameFile,
        _internal_client_types.DeleteFile,
    ]


def test_a_rename_the_editor_cannot_perform_fails_naming_the_operation() -> None:
    """An operation the editor did not declare must fail with an error that
    names it, not be silently dropped -- the caller is waiting on a result."""
    bridge = _FakeBridge(
        supported=frozenset(
            {ResourceOperationKind.CREATE, ResourceOperationKind.DELETE}
        )
    )
    apply_workspace_edit_bridge.install(bridge)
    try:
        with pytest.raises(errors.InternalError, match="rename"):
            asyncio.run(
                _apply_workspace_edit(
                    _internal_client_types.ApplyWorkspaceEditParams(
                        edit=_internal_client_types.WorkspaceEdit(
                            document_changes=[
                                _internal_client_types.RenameFile(
                                    old_uri="file:///c.py", new_uri="file:///d.py"
                                )
                            ]
                        )
                    )
                )
            )
    finally:
        apply_workspace_edit_bridge.reset()


def test_installing_the_slot_makes_the_call_reach_the_editor() -> None:
    """With the slot installed the request reaches the bridge; without it the
    failure is a clear error, never a ``TypeError`` from calling ``None``."""
    bridge = _FakeBridge(supported=frozenset(ResourceOperationKind))
    apply_workspace_edit_bridge.install(bridge)
    try:
        asyncio.run(_apply_workspace_edit(_mixed_params()))
    finally:
        apply_workspace_edit_bridge.reset()

    assert len(bridge.calls) == 1

    with pytest.raises(errors.InternalError):
        asyncio.run(_apply_workspace_edit(_mixed_params()))
