from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

from fine_lint.apply_lint_fixes_files_action import ConvergenceStatus
from fine_lint.lint_action import LintTarget
from fine_lint.lint_fix import LintFix


@dataclasses.dataclass
class ApplyLintFixesRunPayload(code_action.RunActionPayload):
    target: LintTarget = LintTarget.PROJECT
    """Scope: 'project' (default) fixes the whole workspace, 'files' fixes only file_paths."""
    file_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Files to fix (``file://`` URIs). Only used when target is 'files'."""
    project_paths: list[ResourceUri] | None = None
    """Restrict the workspace operation to these project root URIs (``file://`` URIs). None means the whole workspace."""
    kinds: list[str] | None = None
    """LSP code-action kind filter, matched hierarchically (``source.fixAll``
    matches ``source.fixAll.ruff``). None means all kinds."""
    include_unsafe: bool = False
    """When False (default), only ``FixApplicability.SAFE`` fixes are applied.
    When True, ``FixApplicability.UNSAFE`` fixes are applied too.
    ``FixApplicability.DISPLAY_ONLY`` fixes are never applied (ADR-0085 rule 4)."""
    max_passes: int = 3
    """Upper bound on re-fix passes per project (ADR-0085)."""
    dry_run: bool = False
    """Forwarded to ``apply_lint_fixes_files`` per project (ADR-0085 rule 5):
    preview pass 1 only and write nothing. See
    ``ApplyLintFixesFilesRunPayload.dry_run`` for the single-pass limitation."""


@dataclasses.dataclass
class ApplyLintFixesRunResult(code_action.RunActionResult):
    applied_counts: dict[ResourceUri, int] = dataclasses.field(default_factory=dict)
    """Number of fixes actually written to each file, summed across all passes."""

    remaining_fixes: dict[ResourceUri, list[LintFix]] = dataclasses.field(
        default_factory=dict
    )
    """Fixes that did not end up durably applied. See
    ``ApplyLintFixesFilesRunResult.remaining_fixes`` for the exact semantics per
    convergence status."""

    statuses: dict[ResourceUri, ConvergenceStatus] = dataclasses.field(
        default_factory=dict
    )
    """Convergence status per project root URI. The pass loop runs once per
    project over all of that project's requested files together, so status is
    a per-project property, not a per-file one -- one project converging does
    not imply another one did."""

    passes: dict[ResourceUri, int] = dataclasses.field(default_factory=dict)
    """Number of passes run per project root URI."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ApplyLintFixesRunResult):
            return
        # applied_counts/remaining_fixes are keyed by file; statuses/passes are
        # keyed by project root -- both are disjoint across projects, so a
        # plain per-key union is a correct merge of independent contributions
        # (R-306).
        self.applied_counts.update(other.applied_counts)
        self.remaining_fixes.update(other.remaining_fixes)
        self.statuses.update(other.statuses)
        self.passes.update(other.passes)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if any(
            status != ConvergenceStatus.CONVERGED for status in self.statuses.values()
        ):
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class ApplyLintFixesRunContext(
    code_action.RunActionWithPartialResultsContext[ApplyLintFixesRunPayload]
): ...


class ApplyLintFixesAction(
    code_action.Action[
        ApplyLintFixesRunPayload,
        ApplyLintFixesRunContext,
        ApplyLintFixesRunResult,
    ]
):
    """Compute and apply lint fixes across the workspace, converging per project.

    Routes files to their owning projects and runs the ``apply_lint_fixes_files``
    pass loop (ADR-0085) once per project. ``python -m finecode run
    apply_lint_fixes`` is the ``--fix``-equivalent workflow for the whole
    project or workspace.
    """

    DESCRIPTION = "Compute and apply lint fixes across the workspace."
    SCOPE = code_action.ActionScope.WORKSPACE
    PAYLOAD_TYPE = ApplyLintFixesRunPayload
    RUN_CONTEXT_TYPE = ApplyLintFixesRunContext
    RESULT_TYPE = ApplyLintFixesRunResult
