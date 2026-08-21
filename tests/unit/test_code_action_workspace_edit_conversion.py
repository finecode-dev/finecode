"""Converting code-action operations to the ``WorkspaceEdit`` a client can read.

The endpoint speaks ``documentChanges`` to a client that declared support and
``changes`` to one that did not, and drops an action a client provably cannot
apply rather than offering it edit-less (ADR-0089).
"""

from __future__ import annotations

from fine_lint.apply_code_actions_action import (
    CreateFileOperation,
    DeleteFileOperation,
    RenameFileOperation,
    TextEditOperation,
)
from fine_lint.code_action_types import CodeAction
from fine_lint.lint_fix import Position, Range, TextEdit
from lsprotocol import types

from finecode.lsp_server.endpoints.code_actions import (
    _code_action_to_lsp,
    _workspace_edit_to_lsp,
)
from finecode.wm_server.runner.apply_workspace_edit_bridge import (
    ResourceOperationKind,
)

_URI = "file:///tmp/subject.py"
_OTHER_URI = "file:///tmp/other.py"
_EDIT = TextEdit(
    range=Range(start=Position(line=0, character=0), end=Position(line=0, character=1)),
    new_text="X",
)


def _action(*operations: object) -> CodeAction:
    return CodeAction(
        provider="provider_a",
        action_id="a1",
        title="Fix it",
        kind="quickfix",
        operations=list(operations),  # type: ignore[arg-type]
    )


def _text_edit_operation(uri: str = _URI) -> TextEditOperation:
    return TextEditOperation(file_path=uri, edits=[_EDIT])


def test_text_only_action_converts_to_both_forms_with_identical_edits() -> None:
    """The same text edits must survive conversion on either path; a client
    without ``documentChanges`` gets ``changes`` with the same edits."""
    operation = _text_edit_operation()
    capable = _workspace_edit_to_lsp(
        [operation],
        supports_document_changes=True,
        resource_operations=frozenset(),
    )
    incapable = _workspace_edit_to_lsp(
        [operation],
        supports_document_changes=False,
        resource_operations=frozenset(),
    )

    assert capable is not None and capable.document_changes is not None
    document_edit = capable.document_changes[0]
    assert isinstance(document_edit, types.TextDocumentEdit)
    assert document_edit.text_document.version is None
    assert document_edit.text_document.uri == _URI
    assert [edit.new_text for edit in document_edit.edits] == ["X"]

    assert incapable is not None and incapable.changes is not None
    assert [edit.new_text for edit in incapable.changes[_URI]] == ["X"]


def test_rename_drops_without_the_operation_and_converts_with_it() -> None:
    """A rename must not be sent to a client that did not declare the ``rename``
    resource operation, and must convert once it did."""
    operation = RenameFileOperation(old_path=_URI, new_path=_OTHER_URI)
    assert (
        _workspace_edit_to_lsp(
            [operation],
            supports_document_changes=True,
            resource_operations=frozenset({ResourceOperationKind.CREATE}),
        )
        is None
    )
    converted = _workspace_edit_to_lsp(
        [operation],
        supports_document_changes=True,
        resource_operations=frozenset({ResourceOperationKind.RENAME}),
    )
    assert converted is not None and converted.document_changes is not None
    assert isinstance(converted.document_changes[0], types.RenameFile)
    assert converted.document_changes[0].old_uri == _URI
    assert converted.document_changes[0].new_uri == _OTHER_URI


def test_two_ordered_operations_convert_under_document_changes_but_not_changes() -> (
    None
):
    """``documentChanges`` is ordered and can carry two edits to one file; the
    unordered ``changes`` map cannot, so the fallback must drop the action."""
    operations = [_text_edit_operation(), _text_edit_operation()]
    capable = _workspace_edit_to_lsp(
        operations,
        supports_document_changes=True,
        resource_operations=frozenset(),
    )
    incapable = _workspace_edit_to_lsp(
        operations,
        supports_document_changes=False,
        resource_operations=frozenset(),
    )

    assert capable is not None and capable.document_changes is not None
    assert len(capable.document_changes) == 2
    assert incapable is None


def test_every_versioned_text_document_identifier_carries_none() -> None:
    """FineCode's file version is a content hash, not an LSP monotonic document
    version; putting one in the other's field would make an editor reject valid
    edits, so every identifier sent to the client carries ``None``."""
    action = _action(_text_edit_operation())
    converted = _code_action_to_lsp(
        action,
        file_uri=_URI,
        supports_document_changes=True,
        resource_operations=frozenset(),
    )
    assert converted is not None
    assert converted.edit is not None
    assert converted.edit.document_changes is not None
    for document_edit in converted.edit.document_changes:
        assert isinstance(document_edit, types.TextDocumentEdit)
        assert document_edit.text_document.version is None


def test_a_resource_operation_the_client_cannot_perform_drops_the_action() -> None:
    """An action carrying a create for a client that did not declare ``create``
    must be dropped outright, not offered with the create silently removed."""
    action = _action(CreateFileOperation(file_path=_URI))
    converted = _code_action_to_lsp(
        action,
        file_uri=_URI,
        supports_document_changes=True,
        resource_operations=frozenset(),
    )
    assert converted is None
