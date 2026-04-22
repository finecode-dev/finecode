# docs: docs/reference/actions.md
"""
list_tests action — discover tests and return their structure without running them.

Design decisions
----------------
**Payload mirrors RunTestsRunPayload.file_paths only.**
    Discovery is always exhaustive within the given scope. Filtering by
    test_ids or markers at discovery time would return an incomplete tree,
    breaking the TestController's expectation of a full test inventory.
    Use RunTestsAction with test_ids/markers to restrict execution instead.

**TestItem forms a tree, not a flat list.**
    Different test runners produce different hierarchy depths:
    pytest:    file → class (optional) → function
    unittest:  file → class → method  (handler resolves module → file)
    A recursive children list preserves the natural hierarchy without
    callers having to reconstruct it.

**test_id on every node is a valid runnable identifier.**
    Intermediate nodes (file, class) carry a TestId that a handler can
    accept in RunTestsRunPayload.test_ids to run just that scope.
    For example, a file node carries TestId(file_path="tests/test_foo.py")
    and a class node carries TestId(file_path="tests/test_foo.py",
    class_name="MyClass"). See TestId for the full field contract.

**Tree shape is determined by TestId field semantics.**
    Callers (e.g. a VSCode TestController) rely on consistent tree depth
    across handlers. This consistency comes entirely from handlers honouring
    the TestId contract — in particular that class_name is only set when
    there is an actual class in the source, never as synthetic grouping.
    See TestId.class_name for the full constraint.

**file_path is stored as a posix string, line is 0-based.**
    Consistent with TestCaseResult in run_tests and LintMessage in
    lint_files (LSP convention). Handlers sourcing 1-based line numbers
    from CLI output must subtract 1.

**update() appends, it does not deduplicate.**
    Multiple handlers may discover overlapping test suites. Deduplication
    by test_id would silently discard valid results. Handlers that need
    deduplication can do it internally.
"""

from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.testing.test_id import TestId
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class TestItem:
    """A single node in the discovered test tree.

    Leaf nodes represent individual test functions/methods.
    Non-leaf nodes (files, classes) group related tests and carry a
    test_id that can be passed to RunTestsRunPayload.test_ids to run
    the whole group.
    """

    test_id: TestId
    """Unified, handler-agnostic identifier. Handlers convert to/from their native format (e.g. pytest node IDs) when listing and running tests."""

    display_name: str | None = None
    """Human-readable label shown in the test tree. Falls back to the last segment of test_id when None."""

    file_path: ResourceUri | None = None
    """Source location."""

    line: int | None = None
    """0-based line number."""

    children: list['TestItem'] = dataclasses.field(default_factory=list)
    """Child nodes (e.g. test functions inside a class, classes inside a file)."""


@dataclasses.dataclass
class ListTestsRunPayload(code_action.RunActionPayload):
    """Payload for listing tests.
    """
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files or directories to search for tests. Empty list means the handler uses its own configured defaults (e.g. testpaths in pytest.ini)."""


@dataclasses.dataclass
class ListTestsRunResult(code_action.RunActionResult):
    """Result of listing tests.
    """
    tests: list[TestItem]

    def update(self, other: code_action.RunActionResult) -> None:
        """Append test items from another handler's result.

        Results are not deduplicated — two handlers may legitimately
        discover overlapping test suites. See module docstring.
        """
        if not isinstance(other, ListTestsRunResult):
            return
        self.tests.extend(other.tests)

    def to_text(self) -> str | textstyler.StyledText:
        if not self.tests:
            return "No tests found.\n"

        lines: list[str] = []

        def _walk(item: TestItem, indent: int) -> None:
            label = item.display_name or (
                item.test_id.test_name
                or item.test_id.class_name
                or item.test_id.file_path
            )
            prefix = "  " * indent
            location = ""
            if item.file_path is not None:
                location = f"  ({item.file_path}"
                if item.line is not None:
                    location += f":{item.line + 1}"  # display as 1-based
                location += ")"
            lines.append(f"{prefix}{label}{location}")
            for child in item.children:
                _walk(child, indent + 1)

        for root in self.tests:
            _walk(root, 0)

        total = sum(1 for _ in _iter_leaves(self.tests))
        lines.append(f"\n{total} test{'s' if total != 1 else ''} found")
        return "\n".join(lines) + "\n"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


def _iter_leaves(items: list[TestItem]):
    for item in items:
        if item.children:
            yield from _iter_leaves(item.children)
        else:
            yield item


class ListTestsRunContext(code_action.RunActionContext[ListTestsRunPayload]): ...


class ListTestsAction(
    code_action.Action[ListTestsRunPayload, ListTestsRunContext, ListTestsRunResult]
):
    """Discover tests and return their hierarchical structure without running them."""

    PAYLOAD_TYPE = ListTestsRunPayload
    RUN_CONTEXT_TYPE = ListTestsRunContext
    RESULT_TYPE = ListTestsRunResult
