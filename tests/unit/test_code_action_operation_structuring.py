"""Tests for reading a resolved code action off the wire in the LSP endpoint.

``codeAction/resolve`` structures the extension runner's JSON answer back into
``ResolveCodeActionRunResult``, whose ``operations`` is a union of four
dataclasses. cattrs cannot derive a disambiguator for it on its own: its default
needs every arm to own a required field the others lack, and
``CreateFileOperation`` and ``DeleteFileOperation`` share ``file_path`` with
everything else while their remaining fields all have defaults. Without a hook
it raised ``TypeError: ... has no usable non-default attributes`` for every
resolve that actually returned operations -- ``operations=None`` structured
fine, so the failure was invisible until a provider resolved something.
"""

from __future__ import annotations

import cattrs
import pytest

# Imported for its import side effect: registering the structure hook.
import finecode.lsp_server.endpoints.code_actions  # noqa: F401
from fine_lint.apply_code_actions_action import (
    CreateFileOperation,
    DeleteFileOperation,
    RenameFileOperation,
    TextEditOperation,
)
from fine_lint.resolve_code_action_action import ResolveCodeActionRunResult

from finecode._converter import converter

_OPERATIONS = [
    TextEditOperation(file_path="file:///a.py", edits=[], file_version="v1"),
    CreateFileOperation(file_path="file:///a.py"),
    RenameFileOperation(old_path="file:///a.py", new_path="file:///b.py"),
    DeleteFileOperation(file_path="file:///a.py"),
]


@pytest.mark.parametrize("operation", _OPERATIONS, ids=lambda op: type(op).__name__)
def test_each_operation_kind_survives_a_round_trip(operation: object) -> None:
    """Every arm must come back as the class it went out as.

    Trying each arm in turn would not do: cattrs ignores unknown keys, so a
    delete would structure happily as a create and the endpoint would silently
    perform the wrong operation.
    """
    wire = cattrs.Converter().unstructure(operation)

    result = converter.structure(
        {"file_version": "v1", "operations": [wire]}, ResolveCodeActionRunResult
    )

    assert result.operations is not None
    assert type(result.operations[0]) is type(operation)
    assert result.operations[0] == operation


def test_no_operations_still_structures() -> None:
    """The path that always worked must keep working: a provider claiming
    nothing resolves to ``operations=None``."""
    result = converter.structure({"operations": None}, ResolveCodeActionRunResult)

    assert result.operations is None


def test_an_undecidable_operation_is_refused_rather_than_guessed() -> None:
    """A payload naming only the field every arm shares could be a create or a
    delete, and performing the wrong one destroys data. The endpoint catches
    this and degrades to the unresolved action the editor already has."""
    with pytest.raises(cattrs.errors.ClassValidationError):
        converter.structure(
            {"operations": [{"file_path": "file:///a.py"}]},
            ResolveCodeActionRunResult,
        )
