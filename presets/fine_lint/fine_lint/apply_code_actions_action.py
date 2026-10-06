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
    (ADR-0083 rule 1) -- ranges address the content at ``file_version``, and
    are realized by sorting descending by start position and applying
    back-to-front (``_text_edit_algebra.apply_edits``). Do not merge another
    operation's edits into this list to get simultaneity across operations --
    ADR-0083 rule 5 scopes simultaneity to *within* one operation only."""

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

    file_version: str | None = None
    """Guards ``old_path`` against a file that has changed since the caller
    decided to rename it. Same ``None`` semantics as
    ``TextEditOperation.file_version``."""


@dataclasses.dataclass
class DeleteFileOperation:
    file_path: ResourceUri
    """File to delete."""

    file_version: str | None = None
    """Guards the delete against a file that has changed since the caller
    decided to delete it. Same ``None`` semantics as
    ``TextEditOperation.file_version``."""

    missing_ok: bool = False
    """Whether a path that is already gone counts as success rather than
    ``FILE_MISSING`` (LSP ``DeleteFile.options.ignoreIfNotExists``)."""

    recursive: bool = False
    """Whether ``file_path`` names a directory to delete recursively (LSP
    ``DeleteFile.options.recursive``). Requires the run to have opted in via
    ``allow_recursive_delete``; without it the selection is ``REFUSED_UNSAFE``."""


CodeActionOperation = (
    TextEditOperation | CreateFileOperation | RenameFileOperation | DeleteFileOperation
)
"""One step of a code action's effect. A selection carries an ORDERED list of
these (ADR-0083 rule 5) -- file operations force the ordered form, because a
create must precede edits to the file it creates, and a rename precedes or
follows edits depending on which path they name.

``apply_code_actions`` executes all four kinds. A path the batch brings into
existence (a create target or a rename's ``new_path``) has no agreed version,
so an operation naming such a path carries ``file_version=None`` (DA-7)."""


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
    """Ordered effect of this selection (ADR-0083 rule 5). ``None`` means
    apply resolves it via ``provider`` + ``action_id`` first (ADR-0084 rule 3);
    a selection whose resolve finds nothing becomes ``UNRESOLVED`` and does
    not block the rest of the batch."""

    is_preferred: bool = False
    """Tiebreak for greedy selection order (ADR-0083 rule 4): selections are
    accepted in the order they appear in ``selections``, with preferred ones
    moved ahead of non-preferred ones among otherwise-equal candidates."""


class ApplyOutcome(enum.StrEnum):
    APPLIED = "applied"
    """The selection's edits were accepted and written."""

    DEFERRED = "deferred"
    """Valid, but not applied this pass: either it overlapped an
    already-accepted edit (ADR-0083 rule 4), or it shares a batch with a
    selection whose file failed validation and the whole batch was refused
    (ADR-0083). Not a failure -- a re-run is likely to apply it."""

    VERSION_CONFLICT = "version_conflict"
    """An operation's base version disagreed with another operation's for the
    same file, or with the file's actual current content."""

    INVALID_RANGE = "invalid_range"
    """An edit's range is malformed or does not address a real position in the
    file's current content."""

    UNRESOLVED = "unresolved"
    """``operations`` was None and no provider claimed ``action_id``."""

    FILE_EXISTS = "file_exists"
    """An operation named a path that is already occupied, and its
    precondition refuses to overwrite it (``overwrite=False`` on a create or
    rename). Distinct from ``VERSION_CONFLICT``: the caller's action is stale
    (the file it meant to create already exists), so it should be re-derived
    rather than retried against newer content."""

    FILE_MISSING = "file_missing"
    """An operation named a path that does not exist, and its precondition
    requires it to (a text edit, or a delete/rename source without
    ``missing_ok``). Distinct from ``VERSION_CONFLICT``: someone removed the
    file the caller meant to act on, so the caller should be told the file is
    gone rather than that it changed underneath them."""

    UNSUPPORTED_OPERATION = "unsupported_operation"
    """The selection resolved to an operation kind this build does not
    implement (a wire payload from a newer provider). None of the selection's
    operations were performed -- a partially-performed selection is worse than
    a refused one (ADR-0083 rule 5)."""

    WRITE_FAILED = "write_failed"
    """An unexpected error occurred while committing the file."""

    REFUSED_UNSAFE = "refused_unsafe"
    """A recursive delete was requested without the run opting in via
    ``allow_recursive_delete``. A directory has no content hash to guard, and
    one operation can remove work that was never named in the batch -- so the
    refusal is explicit rather than a silent no-op."""

    PARTIALLY_APPLIED = "partially_applied"
    """The selection edits several files; at least one was written and at least
    one failed to write. Distinct from ``WRITE_FAILED`` because the two call for
    opposite responses: a wholly-failed selection can simply be retried, whereas
    re-running this one would apply the already-written files' edits a second
    time. ``file_summaries`` says which files did land. Validation is
    batch-wide (ADR-0083), so this only arises from a write that failed
    after another file's write in the same batch had already succeeded."""


@dataclasses.dataclass
class ApplyCodeActionsRunPayload(code_action.RunActionPayload):
    selections: list[CodeActionSelection]

    dry_run: bool = False
    """When True, validate and compute the batch's result exactly as a real
    run would, but write nothing (ADR-0085 rule 5). Reads use
    ``session.read_file`` rather than claiming files, so a preview never
    blocks a real writer. ``VERSION_CONFLICT`` and every other outcome are
    PREDICTIONS in this mode -- in particular, the file can change between a
    dry run and a later real apply, so ``VERSION_CONFLICT`` is only as
    reliable as that gap is short."""

    allow_recursive_delete: bool = False
    """Opt-in for recursive directory deletes. A recursive delete is the only
    operation that is irreversible, unguarded and unbounded at once -- a
    directory has no content hash, and one operation can remove work never
    named in the batch -- so it is refused with ``REFUSED_UNSAFE`` unless the
    caller passes this explicitly."""


@dataclasses.dataclass
class FileApplySummary:
    written: bool
    """True iff this file's content was committed to disk during this run.
    Always False for a dry run (ADR-0085 rule 5)."""

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
    run) after applying every accepted operation -- ADR-0085 rule 5. Refused
    operations contribute nothing: a file with zero accepted edits still
    appears here with its unchanged content, but a batch refused wholesale by
    validation (ADR-0083) contributes no entries at all. A path the batch
    deletes or renames away is omitted entirely; ``deleted_paths`` says which
    of those were deletions."""

    deleted_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    """Paths the batch would delete (dry run) or did delete (real run). A
    preview needs this rather than having to infer an absence from a missing
    ``resulting_content`` key."""

    dry_run: bool = False
    """Echoes ``ApplyCodeActionsRunPayload.dry_run`` back, because the outcome
    enum above is identical in both modes and a caller must not be able to
    mistake a preview for an application (ADR-0085 rule 5)."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ApplyCodeActionsRunResult):
            return
        self.outcomes.update(other.outcomes)
        self.file_summaries.update(other.file_summaries)
        self.resulting_content.update(other.resulting_content)
        if self.deleted_paths and other.deleted_paths:
            self.deleted_paths = list(
                dict.fromkeys((*self.deleted_paths, *other.deleted_paths))
            )
        else:
            self.deleted_paths = self.deleted_paths or other.deleted_paths
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
      (ADR-0083), mirroring the format pipeline's ``SaveFormatFileHandler``.
    - A selection carries an ORDERED list of operations (ADR-0083 rule 5),
      and all four operation kinds are executed. A selection resolving to an
      operation kind this build does not implement is refused wholesale with
      ``UNSUPPORTED_OPERATION`` -- none of its operations are performed,
      since a partially-performed selection is worse than a refused one.
    - Each operation that guards a path names its own base version (ADR-0083
      rules 1 and 5) -- there is no single version for a whole selection.
      For every path that exists when the batch is claimed, all operations
      across the whole batch that name that path must agree on its version,
      and that agreed version must match the file's actual current content; a
      file whose operations disagree, or whose claimed content no longer
      matches, is refused with ``VERSION_CONFLICT`` for every selection
      touching it. A path the batch brings into existence (a create target or
      a rename's ``new_path``) has no version and must carry none (DA-7); a
      non-``None`` version there is ``VERSION_CONFLICT``.
    - Whole-file operations (create, rename, delete) conflict with every
      other selection touching the paths they name; the first accepted
      selection wins and the loser is ``DEFERRED`` (ADR-0083 rule 4, extended
      to whole files). Within one selection, a create then an edit of the
      created file is the normal chain and both land.
    - The commit replays an ordered journal (ADR-0088): a path's create or
      rename-into step precedes its write step, its delete step follows, and
      every write, create and rename precedes every delete. Validation is
      still atomic (nothing commits unless the whole batch validates), but
      the commit is not: past the first irreversible step a failure reports
      ``PARTIALLY_APPLIED`` rather than pretending the batch was untouched.
    - Multiple operations for the same file within one selection apply in
      list order, each against the content the previous one produced --
      never merged into one simultaneous batch (ADR-0083 rule 5). Operations
      for the same file coming from *different* selections remain
      simultaneous against that file's one agreed base version, exactly as
      before (ADR-0083 rule 1).
    - Edits are accepted greedily in ``selections`` order (``is_preferred``
      first as a tiebreak); an edit overlapping an already-accepted one is
      ``DEFERRED``, not a failure (ADR-0083 rule 4). Two zero-width insertions
      at the same position count as overlapping (ADR-0083 rule 4).
    - Validation happens for the whole batch before any write: if any file
      fails validation, nothing in the batch is written (ADR-0083).
    - A selection with ``operations=None`` is resolved via its ``provider``
      and ``action_id`` first; one that resolves to nothing is ``UNRESOLVED``
      and does not block the rest of the batch.
    - ``dry_run=True`` runs every validation and the whole greedy selection,
      computes each file's resulting content in memory, and writes nothing
      (ADR-0085 rule 5). It reads via ``session.read_file`` instead of
      claiming files, so a preview never blocks a real writer. Every outcome,
      ``VERSION_CONFLICT`` most of all, is a prediction that a real run
      shortly afterwards could still see differently.
    - This action applies exactly what it was handed, once. It does not repeat
      passes or re-derive fixes against the new content -- that is workflow
      semantics, owned by a higher-level action (ADR-0085).
    """

    DESCRIPTION = "Apply a batch of code-action selections to disk."
    PAYLOAD_TYPE = ApplyCodeActionsRunPayload
    RUN_CONTEXT_TYPE = ApplyCodeActionsRunContext
    RESULT_TYPE = ApplyCodeActionsRunResult
