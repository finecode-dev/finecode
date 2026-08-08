from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri

from fine_lint.lint_fix import TextEdit


@dataclasses.dataclass
class TextEditOperation:
    file_path: ResourceUri
    """File this operation edits."""

    edits: list[TextEdit]
    """Edits to apply, interpreted simultaneously against ``file_version``
    (design note D2) -- ranges address the content at ``file_version``, and
    are realized by sorting descending by start position and applying
    back-to-front (``_text_edit_algebra.apply_edits``). Do not merge another
    operation's edits into this list to get simultaneity across operations --
    design note D11 scopes simultaneity to *within* one operation only."""

    file_version: str | None = None
    """Content version ``edits`` were computed against.

    ``None`` means NO staleness guard: the write is conditional only on the
    file not changing between claim and write, which the exclusive claim
    already provides on its own. ``None`` does NOT mean "skip the write" --
    a selection with ``file_version=None`` still writes. A provider that
    computed its edits against specific content MUST supply that version;
    leaving it unset is only correct when the operation has no particular
    base content to be stale against (e.g. it is chained after an earlier
    operation in the same selection whose result was never persisted, so no
    disk version for it exists yet)."""


@dataclasses.dataclass
class CreateFileOperation:
    file_path: ResourceUri
    """File to create."""

    overwrite: bool = False
    """Whether an existing file at ``file_path`` may be overwritten."""


@dataclasses.dataclass
class RenameFileOperation:
    old_path: ResourceUri
    new_path: ResourceUri

    overwrite: bool = False
    """Whether an existing file at ``new_path`` may be overwritten."""


@dataclasses.dataclass
class DeleteFileOperation:
    file_path: ResourceUri
    """File to delete."""

    file_version: str | None = None
    """Guards the delete against a file that has changed since the caller
    decided to delete it. Same ``None`` semantics as
    ``TextEditOperation.file_version``."""


CodeActionOperation = (
    TextEditOperation | CreateFileOperation | RenameFileOperation | DeleteFileOperation
)
"""One step of a code action's effect. A selection carries an ORDERED list of
these (design note D11) -- file operations force the ordered form, because a
create must precede edits to the file it creates, and a rename precedes or
follows edits depending on which path they name.

``apply_code_actions`` executes ``TextEditOperation`` only; a selection
containing any other kind is refused wholesale with ``UNSUPPORTED_OPERATION``
(execution of the others is scoped separately -- ``IFileEditor`` has no
delete or rename yet, and the version of a file that does not exist is
undefined)."""


@dataclasses.dataclass
class CodeActionSelection:
    provider: str
    """Which provider minted ``action_id`` (``CodeAction.provider``)."""

    action_id: str
    """Provider-local identifier, as returned on the original ``CodeAction``."""

    file_path: ResourceUri
    """File the action was originally offered for -- used to resolve and to
    report. This is NOT the set of files the action's operations touch; a
    selection may resolve to operations naming other files entirely (design
    note Q1, e.g. a rename)."""

    operations: list[CodeActionOperation] | None = None
    """Ordered effect of this selection (design note D11). ``None`` means
    apply resolves it via ``provider`` + ``action_id`` first (design note D7);
    a selection whose resolve finds nothing becomes ``UNRESOLVED`` and does
    not block the rest of the batch."""

    is_preferred: bool = False
    """Tiebreak for greedy selection order (design note D3): selections are
    accepted in the order they appear in ``selections``, with preferred ones
    moved ahead of non-preferred ones among otherwise-equal candidates."""


class ApplyOutcome(enum.StrEnum):
    APPLIED = "applied"
    """The selection's edits were accepted and written."""

    DEFERRED = "deferred"
    """Valid, but not applied this pass: either it overlapped an
    already-accepted edit (design notes D3/D4), or it shares a batch with a
    selection whose file failed validation and the whole batch was refused
    (design note D5). Not a failure -- a re-run is likely to apply it."""

    VERSION_CONFLICT = "version_conflict"
    """An operation's base version disagreed with another operation's for the
    same file, or with the file's actual current content."""

    INVALID_RANGE = "invalid_range"
    """An edit's range is malformed or does not address a real position in the
    file's current content."""

    UNRESOLVED = "unresolved"
    """``operations`` was None and no provider claimed ``action_id``."""

    UNSUPPORTED_OPERATION = "unsupported_operation"
    """The selection resolved to at least one operation other than
    ``TextEditOperation`` (a create, rename, or delete). None of the
    selection's operations were performed -- a partially-performed selection
    is worse than a refused one (design note D11)."""

    WRITE_FAILED = "write_failed"
    """An unexpected error occurred while committing the file."""

    PARTIALLY_APPLIED = "partially_applied"
    """The selection edits several files; at least one was written and at least
    one failed to write. Distinct from ``WRITE_FAILED`` because the two call for
    opposite responses: a wholly-failed selection can simply be retried, whereas
    re-running this one would apply the already-written files' edits a second
    time. ``file_summaries`` says which files did land. Validation is
    batch-wide (design note D5), so this only arises from a write that failed
    after another file's write in the same batch had already succeeded."""


@dataclasses.dataclass
class ApplyCodeActionsRunPayload(code_action.RunActionPayload):
    selections: list[CodeActionSelection]

    dry_run: bool = False
    """When True, validate and compute the batch's result exactly as a real
    run would, but write nothing (design note D12). Reads use
    ``session.read_file`` rather than claiming files, so a preview never
    blocks a real writer. ``VERSION_CONFLICT`` and every other outcome are
    PREDICTIONS in this mode -- in particular, the file can change between a
    dry run and a later real apply, so ``VERSION_CONFLICT`` is only as
    reliable as that gap is short."""


@dataclasses.dataclass
class FileApplySummary:
    written: bool
    """True iff this file's content was committed to disk during this run.
    Always False for a dry run (design note D12)."""

    file_version: str | None = None
    """Content version after writing. None when ``written`` is False."""


@dataclasses.dataclass
class ApplyCodeActionsRunResult(code_action.RunActionResult):
    outcomes: dict[int, ApplyOutcome] = dataclasses.field(default_factory=dict)
    """Keyed by a selection's index in
    ``ApplyCodeActionsRunPayload.selections``, so a caller can tell which of
    its selections got which outcome."""

    file_summaries: dict[ResourceUri, FileApplySummary] = dataclasses.field(
        default_factory=dict
    )
    """One entry per file named by any selection's operations."""

    resulting_content: dict[ResourceUri, str] = dataclasses.field(default_factory=dict)
    """The content each touched file would have (dry run) or now has (real
    run) after applying every accepted ``TextEditOperation`` -- design note
    D12. Refused operations contribute nothing: a file with zero accepted
    edits still appears here with its unchanged content, but a batch refused
    wholesale by validation (design note D5) contributes no entries at all."""

    dry_run: bool = False
    """Echoes ``ApplyCodeActionsRunPayload.dry_run`` back, because the outcome
    enum above is identical in both modes and a caller must not be able to
    mistake a preview for an application (design note D12)."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ApplyCodeActionsRunResult):
            return
        self.outcomes.update(other.outcomes)
        self.file_summaries.update(other.file_summaries)
        self.resulting_content.update(other.resulting_content)
        self.dry_run = other.dry_run

    @property
    def return_code(self) -> code_action.RunReturnCode:
        # A deferral is a normal outcome, not a failure -- only outcomes other
        # than APPLIED/DEFERRED indicate something went wrong for this run.
        if any(
            outcome not in (ApplyOutcome.APPLIED, ApplyOutcome.DEFERRED)
            for outcome in self.outcomes.values()
        ):
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class ApplyCodeActionsRunContext(
    code_action.RunActionContext[ApplyCodeActionsRunPayload]
): ...


class ApplyCodeActionsAction(
    code_action.Action[
        ApplyCodeActionsRunPayload,
        ApplyCodeActionsRunContext,
        ApplyCodeActionsRunResult,
    ]
):
    """Apply a batch of code-action selections to disk. The sole writer.

    **Action contract** -- observable guarantees to callers:

    - Providers never write to disk; this action is the only one that does
      (design note D5), mirroring the format pipeline's ``SaveFormatFileHandler``.
    - A selection carries an ORDERED list of operations (design note D11).
      Only ``TextEditOperation`` is executed; a selection resolving to any
      ``CreateFileOperation``/``RenameFileOperation``/``DeleteFileOperation``
      is refused wholesale with ``UNSUPPORTED_OPERATION`` -- none of its
      operations are performed, since a partially-performed selection is
      worse than a refused one.
    - Each ``TextEditOperation`` names its own file and its own base version
      (design note D2/D11) -- there is no single version for a whole
      selection. All operations across the whole batch that name a given
      file must agree on that file's version, and that agreed version must
      match the file's actual current content; a file whose operations
      disagree, or whose claimed content no longer matches, is refused with
      ``VERSION_CONFLICT`` for every selection touching it.
    - Multiple operations for the same file within one selection apply in
      list order, each against the content the previous one produced --
      never merged into one simultaneous batch (design note D11). Operations
      for the same file coming from *different* selections remain
      simultaneous against that file's one agreed base version, exactly as
      before (design note D2).
    - Edits are accepted greedily in ``selections`` order (``is_preferred``
      first as a tiebreak); an edit overlapping an already-accepted one is
      ``DEFERRED``, not a failure (design note D3). Two zero-width insertions
      at the same position count as overlapping (design note D4).
    - Validation happens for the whole batch before any write: if any file
      fails validation, nothing in the batch is written (design note D5).
    - A selection with ``operations=None`` is resolved via its ``provider``
      and ``action_id`` first; one that resolves to nothing is ``UNRESOLVED``
      and does not block the rest of the batch.
    - ``dry_run=True`` runs every validation and the whole greedy selection,
      computes each file's resulting content in memory, and writes nothing
      (design note D12). It reads via ``session.read_file`` instead of
      claiming files, so a preview never blocks a real writer. Every outcome,
      ``VERSION_CONFLICT`` most of all, is a prediction that a real run
      shortly afterwards could still see differently.
    - This action applies exactly what it was handed, once. It does not repeat
      passes or re-derive fixes against the new content -- that is workflow
      semantics, owned by a higher-level action (design note D8).
    """

    DESCRIPTION = "Apply a batch of code-action selections to disk."
    PAYLOAD_TYPE = ApplyCodeActionsRunPayload
    RUN_CONTEXT_TYPE = ApplyCodeActionsRunContext
    RESULT_TYPE = ApplyCodeActionsRunResult
