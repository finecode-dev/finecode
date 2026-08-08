from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

from fine_lint.lint_fix import LintFix


@dataclasses.dataclass
class ApplyLintFixesFilesRunPayload(code_action.RunActionPayload):
    file_paths: list[ResourceUri]
    """Files to compute and apply lint fixes for."""

    kinds: list[str] | None = None
    """LSP code-action kind filter, matched hierarchically (``source.fixAll``
    matches ``source.fixAll.ruff``). None means all kinds."""

    include_unsafe: bool = False
    """When False (default), only ``FixApplicability.SAFE`` fixes are applied.
    When True, ``FixApplicability.UNSAFE`` fixes are applied too.
    ``FixApplicability.DISPLAY_ONLY`` fixes are never applied, regardless of this
    flag (design note D10)."""

    max_passes: int = 3
    """Upper bound on re-fix passes (design note D8). Each pass re-requests fixes
    against the files' latest content, since an earlier pass's edits may reveal
    fixes that were not visible before, or invalidate fixes another provider
    offered. Exhausting this bound without converging is reported as
    ``ConvergenceStatus.MAX_PASSES_REACHED`` rather than looping indefinitely."""

    dry_run: bool = False
    """When True, run exactly ONE pass through ``apply_code_actions`` in its
    own dry-run mode and write nothing (design note D12). This previews pass 1
    only: simulating later passes would require ``get_lint_fixes`` to run
    against simulated content, but providers read through the file editor
    rather than an injected string, so there is no simulated content to hand
    them. The result's ``status`` is ``ConvergenceStatus.PREVIEWED`` --
    CONVERGED/OSCILLATED/MAX_PASSES_REACHED all claim something about the
    *real*, possibly multi-pass outcome that one preview pass cannot know."""


class ConvergenceStatus(enum.StrEnum):
    CONVERGED = "converged"
    """A pass applied zero edits -- no further progress is possible without new
    input. The normal, successful exit (design note D8).

    Also reported when the last pass in the budget applied every candidate it
    found and left nothing over: the loop stopped because it ran out of passes,
    but it did so with no unapplied work, which is a successful run rather than
    the truncated one ``MAX_PASSES_REACHED`` describes."""

    OSCILLATED = "oscillated"
    """A pass produced a per-file content hash already seen in an earlier pass --
    two fixes are undoing each other. Detected and reported immediately rather
    than exhausting ``max_passes``."""

    MAX_PASSES_REACHED = "max_passes_reached"
    """``max_passes`` was exhausted while fixes were still being applied each
    pass, with no convergence and no detected oscillation."""

    PREVIEWED = "previewed"
    """Terminal status for a dry run (design note D12): pass 1's outcomes and
    ``ApplyLintFixesFilesRunResult.resulting_content`` are a prediction of
    what a real run would do right now, not an applied result. Never used
    for a real (non-dry-run) run -- CONVERGED/OSCILLATED/MAX_PASSES_REACHED
    all describe something a single preview pass cannot determine."""


@dataclasses.dataclass
class ApplyLintFixesFilesRunResult(code_action.RunActionResult):
    applied_counts: dict[ResourceUri, int] = dataclasses.field(default_factory=dict)
    """Number of fixes actually written to each file, summed across all passes."""

    remaining_fixes: dict[ResourceUri, list[LintFix]] = dataclasses.field(
        default_factory=dict
    )
    """Fixes that did not end up durably applied.

    For ``CONVERGED``/``MAX_PASSES_REACHED`` these are the fixes left over from
    the pass where the loop stopped (deferred, conflicted, or otherwise not
    applied). For ``OSCILLATED`` these are instead the fixes that *were* applied
    during the oscillating pass -- the ones whose combined effect reverted a
    file to content already seen in an earlier pass, naming what caused the
    oscillation."""

    status: ConvergenceStatus = ConvergenceStatus.CONVERGED

    passes: int = 0
    """Number of passes actually run before the loop stopped."""

    resulting_content: dict[ResourceUri, str] = dataclasses.field(default_factory=dict)
    """Only populated for a dry run (``status == PREVIEWED``, design note
    D12) -- the content each touched file would have after pass 1. The
    normal, real-write pass loop never fills this in."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ApplyLintFixesFilesRunResult):
            return
        self.applied_counts.update(other.applied_counts)
        self.remaining_fixes.update(other.remaining_fixes)
        self.resulting_content.update(other.resulting_content)
        # A single handler runs the whole pass loop in one shot -- there is only
        # ever one contribution with a meaningful status/passes for this run.
        self.status = other.status
        self.passes = other.passes

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.status != ConvergenceStatus.CONVERGED:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class ApplyLintFixesFilesRunContext(
    code_action.RunActionContext[ApplyLintFixesFilesRunPayload]
): ...


class ApplyLintFixesFilesAction(
    code_action.Action[
        ApplyLintFixesFilesRunPayload,
        ApplyLintFixesFilesRunContext,
        ApplyLintFixesFilesRunResult,
    ]
):
    """Compute and apply lint fixes for specific files, repeating until stable.

    Internal action dispatched by ``apply_lint_fixes``. Owns the re-fix pass
    loop (design note D8): each pass calls ``get_lint_fixes`` per file, filters
    by applicability and kind (design note D10), and applies the survivors as
    one ``apply_code_actions`` batch. Re-requesting fixes each pass re-derives
    positions for free and drops fixes another provider's edits invalidated --
    the only available answer to semantic interference between providers
    (design note, section 2). The loop stops on the first of: a pass that
    applies nothing (``CONVERGED``), a pass whose resulting content for some
    file repeats a hash already seen in an earlier pass (``OSCILLATED``), or
    ``max_passes`` (``MAX_PASSES_REACHED``). ``dry_run=True`` short-circuits
    all of that and previews pass 1 only, terminal status ``PREVIEWED``
    (design note D12).
    """

    DESCRIPTION = "Compute and apply lint fixes for specific files, repeating passes until stable."
    PAYLOAD_TYPE = ApplyLintFixesFilesRunPayload
    RUN_CONTEXT_TYPE = ApplyLintFixesFilesRunContext
    RESULT_TYPE = ApplyLintFixesFilesRunResult
