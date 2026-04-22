# docs: docs/reference/actions.md
"""
run_tests action — execute tests and collect structured results.

Design decisions
----------------
**Payload is intentionally minimal.**
    Whether a test suite is "unit", "integration", or "e2e" is a handler/project
    configuration concern, not a payload concern. Callers use ``markers`` for any
    filtering that is meaningful at invocation time (e.g. ``--markers=unit``).
    Handlers map markers to runner-specific filter flags (pytest ``-m``, etc.).

**test_ids use the unified TestId schema.**
    Callers pass ``TestId`` objects obtained from ``ListTestsRunResult``
    (or constructed directly using the structured fields). Handlers are
    responsible for converting ``TestId`` to and from their native runner
    format (e.g. pytest node IDs, unittest dotted names) internally.

**TestCaseResult.line is 0-based.**
    Consistent with ``Position.line`` used throughout lint actions (LSP convention).
    Handlers that consume 1-based line numbers from CLI output must subtract 1.

**No stdout/stderr capture in the result.**
    The result focuses on structured, machine-readable test outcomes. Raw output
    collection is the responsibility of a separate action or handler concern.
    This keeps the result schema stable and avoids unbounded blobs in memory.

**Plain RunActionContext, not RunActionWithPartialResultsContext.**
    Test runners produce results as a batch at the end of a run, not as an
    incremental stream. If streaming becomes needed in the future the context
    can be promoted to ``RunActionWithPartialResultsContext`` without breaking
    existing handlers.

**update() appends, it does not deduplicate.**
    Multiple handlers may run the same logical test suite with different runners
    or configurations. Deduplication by test_id would silently discard valid
    results. Handlers that need deduplication can do it internally.
"""

from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.actions.testing.test_id import TestId
from finecode_extension_api.resource_uri import ResourceUri


class TestOutcome(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclasses.dataclass
class TestCaseResult:
    """Result for a single test case.
    """

    test_id: TestId
    """Unified, handler-agnostic identifier. Handlers convert to/from their native format (e.g. pytest node IDs) when listing and running tests."""

    outcome: TestOutcome
    """Outcome of the test case (passed, failed, skipped, error)."""

    display_name: str | None = None
    """Human-readable name shown in output. Falls back to test_id when None."""

    duration_seconds: float | None = None
    """Wall-clock duration of this test case."""

    message: str | None = None
    """Failure or error message (traceback, assertion text, etc.). Only expected when outcome is FAILED or ERROR."""

    file_path: ResourceUri | None = None
    """Source file."""

    line: int | None = None
    """0-based line number."""


@dataclasses.dataclass
class RunTestsRunPayload(code_action.RunActionPayload):
    """Payload for running tests.
    """
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Test files or directories to run. Empty list means the handler uses its own configured defaults (e.g. testpaths in pytest.ini)."""
    test_ids: list[TestId] = dataclasses.field(default_factory=list)
    """Unified test identifiers to restrict execution to. Obtained from ListTestsRunResult or constructed directly. Handlers convert to their native format internally."""
    markers: list[str] = dataclasses.field(default_factory=list)
    """Marker/tag names to filter the test suite. Common values: 'unit', 'integration', 'e2e', 'slow'. Handlers map these to their runner's filter flags."""


@dataclasses.dataclass
class RunTestsRunResult(code_action.RunActionResult):
    """Result of running tests.
    """
    test_results: list[TestCaseResult]
    """List of results for each test case."""

    # --- convenience counts ---

    @property
    def passed(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.SKIPPED)

    @property
    def errors(self) -> int:
        return sum(1 for t in self.test_results if t.outcome == TestOutcome.ERROR)

    # --- RunActionResult interface ---

    def update(self, other: code_action.RunActionResult) -> None:
        """Append test results from another handler's result.

        Results are not deduplicated — two handlers may legitimately run the
        same test in different configurations. See module docstring.
        """
        if not isinstance(other, RunTestsRunResult):
            return
        self.test_results.extend(other.test_results)

    def to_text(self) -> str | textstyler.StyledText:
        text = textstyler.StyledText()

        if not self.test_results:
            text.append("No tests were collected.\n")
            return text

        # Summary line
        summary_color = (
            textstyler.Color.GREEN
            if self.failed == 0 and self.errors == 0
            else textstyler.Color.RED
        )
        parts = []
        if self.passed:
            parts.append(f"{self.passed} passed")
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.errors:
            parts.append(f"{self.errors} error{'s' if self.errors != 1 else ''}")
        text.append_styled(", ".join(parts) + "\n", foreground=summary_color, bold=True)

        # Failure / error details
        problem_results = [
            t
            for t in self.test_results
            if t.outcome in (TestOutcome.FAILED, TestOutcome.ERROR)
        ]
        for result in problem_results:
            label = "FAILED" if result.outcome == TestOutcome.FAILED else "ERROR"
            name = result.display_name or str(result.test_id)
            text.append_styled(
                f"\n{label} ", foreground=textstyler.Color.RED, bold=True
            )
            text.append_styled(name, bold=True)
            if result.file_path is not None:
                location = result.file_path
                if result.line is not None:
                    location += f":{result.line + 1}"  # display as 1-based
                text.append(f" ({location})")
            if result.message:
                text.append(f"\n{result.message}\n")
            else:
                text.append("\n")

        return text

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if any(
            t.outcome in (TestOutcome.FAILED, TestOutcome.ERROR)
            for t in self.test_results
        ):
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class RunTestsRunContext(code_action.RunActionContext[RunTestsRunPayload]): ...


class RunTestsAction(
    code_action.Action[RunTestsRunPayload, RunTestsRunContext, RunTestsRunResult]
):
    """Execute tests and return structured pass/fail results."""

    PAYLOAD_TYPE = RunTestsRunPayload
    RUN_CONTEXT_TYPE = RunTestsRunContext
    RESULT_TYPE = RunTestsRunResult
