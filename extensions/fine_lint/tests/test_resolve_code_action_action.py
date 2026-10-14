"""Tests for ``ResolveCodeActionRunResult`` merging."""

from __future__ import annotations

from fine_lint.apply_code_actions_action import TextEditOperation
from fine_lint.resolve_code_action_action import ResolveCodeActionRunResult


def test_a_later_none_contribution_does_not_erase_a_resolved_result() -> None:
    """Exactly one provider owns a given action_id (ADR-0084); a resolve
    result that already found the operations must not be blanked out by
    another provider's "not mine" answer merging in afterwards, or an editor
    applying the result would see no effect for an action it was told exists.
    """
    accumulated = ResolveCodeActionRunResult(file_version="v1", operations=[])
    non_owner = ResolveCodeActionRunResult(file_version="v1", operations=None)

    accumulated.update(non_owner)

    assert accumulated.operations == []


def test_a_later_non_none_contribution_fills_in_an_unresolved_result() -> None:
    """When the owning provider's contribution merges in after a non-owner's,
    the caller must still get the operations -- concurrent handlers complete
    in no guaranteed order, so resolve must not depend on which handler's
    result happens to merge first.
    """
    resolved_operations = [TextEditOperation(file_path="file:///a.py", edits=[])]
    accumulated = ResolveCodeActionRunResult(file_version="v1", operations=None)
    owner = ResolveCodeActionRunResult(
        file_version="v1", operations=resolved_operations
    )

    accumulated.update(owner)

    assert accumulated.operations == resolved_operations
