from __future__ import annotations

import contextlib
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, ilogger, iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_lint import _text_edit_algebra
from fine_lint.apply_code_actions_action import (
    ApplyCodeActionsAction,
    ApplyCodeActionsRunContext,
    ApplyCodeActionsRunPayload,
    ApplyCodeActionsRunResult,
    ApplyOutcome,
    CodeActionOperation,
    CreateFileOperation,
    DeleteFileOperation,
    FileApplySummary,
    RenameFileOperation,
    TextEditOperation,
)
from fine_lint.lint_fix import TextEdit
from fine_lint.resolve_code_action_action import (
    ResolveCodeActionAction,
    ResolveCodeActionRunPayload,
)

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="ApplyCodeActionsHandler")


def _operation_paths(op: CodeActionOperation) -> list[ResourceUri]:
    """Every path an operation names, for claim, conflict and retag purposes."""
    if isinstance(op, RenameFileOperation):
        return [op.old_path, op.new_path]
    return [op.file_path]


def _whole_file_paths(op: CodeActionOperation) -> list[ResourceUri]:
    """The paths a whole-file operation takes over, in whole-file conflict terms."""
    if isinstance(op, TextEditOperation):
        return []
    return _operation_paths(op)


def _version_subject(op: CodeActionOperation) -> ResourceUri | None:
    """The path an operation's ``file_version`` guards, or None for a create."""
    if isinstance(op, RenameFileOperation):
        return op.old_path
    if isinstance(op, CreateFileOperation):
        return None
    return op.file_path


def _op_file_version(op: CodeActionOperation) -> str | None:
    if isinstance(op, CreateFileOperation):
        return None
    return op.file_version


@dataclasses.dataclass
class _JournalStep:
    """One commit step of the ordered journal (ADR-0088)."""

    kind: str
    path: ResourceUri
    new_path: ResourceUri | None = None
    content: str | None = None
    overwrite: bool = False
    if_version: str | None = None
    missing_ok: bool = False
    recursive: bool = False


@dataclasses.dataclass
class ApplyCodeActionsHandlerConfig(code_action.ActionHandlerConfig): ...


class ApplyCodeActionsHandler(
    code_action.ActionHandler[ApplyCodeActionsAction, ApplyCodeActionsHandlerConfig]
):
    """The sole writer for code-action edits.

    Runs the whole batch through, in order: resolve any stubs, refuse any
    selection resolving to an unknown operation kind, claim every target file
    (sorted, to avoid deadlocking against a concurrent apply over an
    overlapping file set) -- or merely read them for a dry run (ADR-0085
    rule 5) -- validate the whole batch before writing anything (ADR-0083),
    accept edits greedily per ADR-0083 rules 1 and 4 and whole-file
    operations per ADR-0088, compute the ordered journal, and commit it (or,
    for a dry run, only compute the result).
    """

    def __init__(
        self,
        file_editor: ifileeditor.IFileEditor,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.file_editor = file_editor
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: ApplyCodeActionsRunPayload,
        run_context: ApplyCodeActionsRunContext,
    ) -> ApplyCodeActionsRunResult:
        selections = payload.selections
        outcomes: dict[int, ApplyOutcome] = {}
        resolved_ops: dict[int, list[CodeActionOperation]] = {}

        # (1) Resolve stubs. A selection whose provider claims nothing becomes
        # UNRESOLVED and simply does not participate further -- it must not
        # block the rest of the batch.
        for index, selection in enumerate(selections):
            if selection.operations is not None:
                operations: list[CodeActionOperation] = selection.operations
            else:
                resolve_result = await self.action_runner.run_action(
                    action_type=iprojectactionrunner.ActionRef.from_type(
                        ResolveCodeActionAction
                    ),
                    payload=ResolveCodeActionRunPayload(
                        provider=selection.provider,
                        action_id=selection.action_id,
                        file_path=selection.file_path,
                    ),
                    meta=run_context.meta,
                )
                if resolve_result.operations is None:
                    outcomes[index] = ApplyOutcome.UNRESOLVED
                    continue
                operations = resolve_result.operations

            # An operation kind this build does not implement (a wire payload
            # from a newer provider) is refused wholesale -- none of its
            # operations are performed, since a partially-performed selection
            # is worse than a refused one (ADR-0083 rule 5).
            if any(
                not isinstance(
                    op,
                    (
                        TextEditOperation,
                        CreateFileOperation,
                        RenameFileOperation,
                        DeleteFileOperation,
                    ),
                )
                for op in operations
            ):
                outcomes[index] = ApplyOutcome.UNSUPPORTED_OPERATION
                continue

            if not operations:
                # Resolved, but to nothing at all -- there is no write to
                # report. It gets no outcome, exactly as when the whole batch
                # resolves to nothing (the `not target_files` return below).
                # It must not fall through to the greedy pass, where an empty
                # operation list trivially conflicts with nothing and would be
                # marked APPLIED: a caller's re-fix loop counts APPLIED as
                # progress, spends passes on a file whose content never
                # changes, and reports the unchanged content as oscillation.
                continue

            resolved_ops[index] = operations

        target_files = sorted(
            {
                path
                for ops in resolved_ops.values()
                for op in ops
                for path in _operation_paths(op)
            },
            key=resource_uri_to_path,
        )
        if not target_files:
            return ApplyCodeActionsRunResult(
                outcomes=outcomes, file_summaries={}, dry_run=payload.dry_run
            )

        created_paths = {
            op.file_path
            for ops in resolved_ops.values()
            for op in ops
            if isinstance(op, CreateFileOperation)
        }
        rename_targets = {
            op.new_path
            for ops in resolved_ops.values()
            for op in ops
            if isinstance(op, RenameFileOperation)
        }
        brought_into_existence = created_paths | rename_targets
        recursive_delete_paths = {
            op.file_path
            for ops in resolved_ops.values()
            for op in ops
            if isinstance(op, DeleteFileOperation) and op.recursive
        }

        # (2) Claim every target file, in sorted path order -- required so two
        # concurrent applies over overlapping file sets cannot deadlock
        # against each other by claiming in different orders. A path the batch
        # may bring into existence is claimed with `claim_file`, which
        # tolerates absence; everything else must exist. A dry run only reads:
        # it must never hold an exclusive claim just to compute a preview
        # (ADR-0085 rule 5).
        async with (
            self.file_editor.session(_FILE_OPERATION_AUTHOR) as session,
            contextlib.AsyncExitStack() as stack,
        ):
            claimed: dict[ResourceUri, ifileeditor.FileInfo | None] = {}
            claim_failures: dict[ResourceUri, ApplyOutcome] = {}
            for file_path in target_files:
                path = resource_uri_to_path(file_path)
                if payload.dry_run:
                    if file_path in recursive_delete_paths:
                        claimed[file_path] = None
                    elif await session.file_exists(path):
                        claimed[file_path] = await stack.enter_async_context(
                            session.read_file(path)
                        )
                    else:
                        claimed[file_path] = None
                elif (
                    file_path in brought_into_existence
                    or file_path in recursive_delete_paths
                ):
                    claimed[file_path] = await stack.enter_async_context(
                        session.claim_file(path)
                    )
                else:
                    try:
                        claimed[file_path] = await stack.enter_async_context(
                            session.modify_file(path)
                        )
                    except ifileeditor.FileNotFound:
                        claim_failures[file_path] = ApplyOutcome.FILE_MISSING
                        claimed[file_path] = None

            # (3) Validate the whole batch before any write. If any file
            # fails, nothing is written -- not even a file that itself
            # validated cleanly (ADR-0083).
            failed_files: dict[ResourceUri, ApplyOutcome] = dict(claim_failures)

            # Version agreement is batch-wide and per file. A path the batch
            # brings into existence has no version to agree with (DA-7): every
            # operation guarding it must carry None.
            for file_path in target_files:
                if file_path in failed_files:
                    continue
                versioned = [
                    op
                    for ops in resolved_ops.values()
                    for op in ops
                    if _version_subject(op) == file_path
                ]
                if file_path in brought_into_existence:
                    if any(_op_file_version(op) is not None for op in versioned):
                        failed_files[file_path] = ApplyOutcome.VERSION_CONFLICT
                    continue

                info = claimed[file_path]
                if info is None:
                    continue
                versions = {
                    version
                    for op in versioned
                    if (version := _op_file_version(op)) is not None
                }
                if len(versions) > 1:
                    failed_files[file_path] = ApplyOutcome.VERSION_CONFLICT
                    continue
                agreed_version = next(iter(versions), None)
                if agreed_version is not None and agreed_version != info.version:
                    failed_files[file_path] = ApplyOutcome.VERSION_CONFLICT

            # Per-selection consistency: each selection's operations are walked
            # in list order against a provisional content that file operations
            # update (create -> "", rename -> source content / absent, delete
            # -> absent). Ranges and existence preconditions are checked here,
            # before greedy selection, so a selection that cannot apply at all
            # fails rather than being deferred for the wrong reason.
            def _provisional(
                provisional: dict[ResourceUri, str | None],
                file_path: ResourceUri,
            ) -> str | None:
                if file_path in provisional:
                    return provisional[file_path]
                info = claimed.get(file_path)
                return info.content if info is not None else None

            for index, ops in resolved_ops.items():
                if index in outcomes:
                    continue
                provisional: dict[ResourceUri, str | None] = {}
                for op in ops:
                    if isinstance(op, TextEditOperation):
                        content = _provisional(provisional, op.file_path)
                        if content is None:
                            failed_files.setdefault(
                                op.file_path, ApplyOutcome.FILE_MISSING
                            )
                            break
                        if any(
                            not _text_edit_algebra.is_valid_edit_range(
                                edit.range, content
                            )
                            for edit in op.edits
                        ):
                            failed_files.setdefault(
                                op.file_path, ApplyOutcome.INVALID_RANGE
                            )
                            break
                    elif isinstance(op, CreateFileOperation):
                        if (
                            _provisional(provisional, op.file_path) is not None
                            and not op.overwrite
                        ):
                            failed_files.setdefault(
                                op.file_path, ApplyOutcome.FILE_EXISTS
                            )
                            break
                        provisional[op.file_path] = ""
                    elif isinstance(op, RenameFileOperation):
                        old_content = _provisional(provisional, op.old_path)
                        if old_content is None:
                            failed_files.setdefault(
                                op.old_path, ApplyOutcome.FILE_MISSING
                            )
                            break
                        if (
                            _provisional(provisional, op.new_path) is not None
                            and not op.overwrite
                        ):
                            failed_files.setdefault(
                                op.new_path, ApplyOutcome.FILE_EXISTS
                            )
                            break
                        provisional[op.new_path] = old_content
                        provisional[op.old_path] = None
                    elif isinstance(op, DeleteFileOperation):
                        if op.recursive and not payload.allow_recursive_delete:
                            failed_files.setdefault(
                                op.file_path, ApplyOutcome.REFUSED_UNSAFE
                            )
                            break
                        if op.recursive:
                            provisional[op.file_path] = None
                            continue
                        if (
                            _provisional(provisional, op.file_path) is None
                            and not op.missing_ok
                        ):
                            failed_files.setdefault(
                                op.file_path, ApplyOutcome.FILE_MISSING
                            )
                            break
                        provisional[op.file_path] = None

            if failed_files:
                for file_path, outcome in failed_files.items():
                    for index, ops in resolved_ops.items():
                        if index in outcomes:
                            continue
                        if any(file_path in _operation_paths(op) for op in ops):
                            outcomes[index] = outcome
                # Every other still-undecided selection was valid on its own
                # but the batch as a whole was refused -- a re-run, once the
                # offending file is fixed, will likely apply it.
                for index in resolved_ops:
                    outcomes.setdefault(index, ApplyOutcome.DEFERRED)
                return ApplyCodeActionsRunResult(
                    outcomes=outcomes,
                    file_summaries={
                        file_path: FileApplySummary(written=False)
                        for file_path in target_files
                    },
                    dry_run=payload.dry_run,
                )

            # (4) Select greedily: caller order, is_preferred first as a
            # tiebreak. Text edits overlap-defer as before (ADR-0083 rules 1
            # and 4); a whole-file operation (create, rename, delete) takes
            # its paths whole and conflicts with every other selection
            # touching them (ADR-0088).
            accepted_by_file: dict[ResourceUri, list[TextEdit]] = {
                file_path: [] for file_path in target_files
            }
            chained_ops_by_index: dict[
                int, dict[ResourceUri, list[TextEditOperation]]
            ] = {}
            whole_file_claims: dict[ResourceUri, int] = {}
            candidate_indices = [
                index for index in resolved_ops if index not in outcomes
            ]
            ordered_indices = sorted(
                candidate_indices,
                key=lambda index: 0 if selections[index].is_preferred else 1,
            )
            for index in ordered_indices:
                first_op_by_file: dict[ResourceUri, TextEditOperation] = {}
                chained_by_file: dict[ResourceUri, list[TextEditOperation]] = {}
                whole_paths: set[ResourceUri] = set()
                all_paths: set[ResourceUri] = set()
                for op in resolved_ops[index]:
                    all_paths.update(_operation_paths(op))
                    if isinstance(op, TextEditOperation):
                        if op.file_path not in first_op_by_file:
                            first_op_by_file[op.file_path] = op
                        else:
                            chained_by_file.setdefault(op.file_path, []).append(op)
                    else:
                        whole_paths.update(_whole_file_paths(op))

                if any(path in whole_file_claims for path in all_paths):
                    outcomes[index] = ApplyOutcome.DEFERRED
                    continue
                if any(accepted_by_file.get(path) for path in whole_paths):
                    outcomes[index] = ApplyOutcome.DEFERRED
                    continue
                conflicts = any(
                    _text_edit_algebra.edits_conflict(
                        op.edits, accepted_by_file.get(file_path, [])
                    )
                    for file_path, op in first_op_by_file.items()
                )
                if conflicts:
                    outcomes[index] = ApplyOutcome.DEFERRED
                    continue

                for file_path, op in first_op_by_file.items():
                    accepted_by_file.setdefault(file_path, []).extend(op.edits)
                if chained_by_file:
                    chained_ops_by_index[index] = chained_by_file
                for path in whole_paths:
                    whole_file_claims[path] = index
                outcomes[index] = ApplyOutcome.APPLIED

            # (5) Overlay: final content per path, absent where the path is
            # removed. Text content keeps the two-tier rule -- simultaneous
            # edits applied to the base in one step, then chained edits in
            # accepted order -- so cross-selection edits stay simultaneous
            # (ADR-0083 rule 1) rather than being sequenced.
            created_paths: set[ResourceUri] = set()
            rename_into: dict[ResourceUri, ResourceUri] = {}
            removed: set[ResourceUri] = set()
            for index in ordered_indices:
                if outcomes.get(index) != ApplyOutcome.APPLIED:
                    continue
                for op in resolved_ops[index]:
                    if isinstance(op, CreateFileOperation):
                        created_paths.add(op.file_path)
                        rename_into.pop(op.file_path, None)
                        removed.discard(op.file_path)
                    elif isinstance(op, RenameFileOperation):
                        rename_into[op.new_path] = op.old_path
                        created_paths.discard(op.new_path)
                        removed.add(op.old_path)
                        removed.discard(op.new_path)
                    elif isinstance(op, DeleteFileOperation):
                        removed.add(op.file_path)
                        created_paths.discard(op.file_path)
                        rename_into.pop(op.file_path, None)

            text_result_cache: dict[ResourceUri, str | None] = {}
            _computing: set[ResourceUri] = set()

            def _text_result(file_path: ResourceUri) -> str | None:
                if file_path in text_result_cache:
                    return text_result_cache[file_path]
                if file_path in _computing:
                    return None
                _computing.add(file_path)
                try:
                    if file_path in created_paths:
                        base = ""
                    elif file_path in rename_into:
                        base = _text_result(rename_into[file_path])
                    else:
                        info = claimed.get(file_path)
                        base = info.content if info is not None else None
                    if base is None:
                        result = None
                    else:
                        content = base
                        accepted_edits = accepted_by_file.get(file_path, [])
                        if accepted_edits:
                            content = _text_edit_algebra.apply_edits(
                                base, accepted_edits
                            )
                            for index in ordered_indices:
                                if outcomes.get(index) != ApplyOutcome.APPLIED:
                                    continue
                                for chained_op in chained_ops_by_index.get(
                                    index, {}
                                ).get(file_path, []):
                                    content = _text_edit_algebra.apply_edits(
                                        content, chained_op.edits
                                    )
                        result = content
                    text_result_cache[file_path] = result
                    return result
                finally:
                    _computing.discard(file_path)

            overlay: dict[ResourceUri, str | None] = {}
            for file_path in target_files:
                if file_path in removed:
                    overlay[file_path] = None
                else:
                    overlay[file_path] = _text_result(file_path)

            # (6) Journal: an ordered list of commit steps. A path's create or
            # rename-into precedes its write; its delete follows; writes,
            # creates and renames all precede deletes (ADR-0088).
            journal: list[_JournalStep] = []
            delete_steps: list[_JournalStep] = []
            written_files: set[ResourceUri] = set()

            def _has_text_edits(file_path: ResourceUri) -> bool:
                return bool(accepted_by_file.get(file_path)) or any(
                    file_path in chained for chained in chained_ops_by_index.values()
                )

            def _emit_write(file_path: ResourceUri) -> None:
                if file_path in written_files:
                    return
                content = overlay.get(file_path)
                if content is None or not _has_text_edits(file_path):
                    return
                info = claimed[file_path]
                journal.append(
                    _JournalStep(
                        kind="write",
                        path=file_path,
                        content=content,
                        if_version=info.version if info is not None else None,
                    )
                )
                written_files.add(file_path)

            for index in ordered_indices:
                if outcomes.get(index) != ApplyOutcome.APPLIED:
                    continue
                for op in resolved_ops[index]:
                    if isinstance(op, CreateFileOperation):
                        journal.append(
                            _JournalStep(
                                kind="create",
                                path=op.file_path,
                                overwrite=op.overwrite,
                            )
                        )
                    elif isinstance(op, RenameFileOperation):
                        journal.append(
                            _JournalStep(
                                kind="rename",
                                path=op.old_path,
                                new_path=op.new_path,
                                overwrite=op.overwrite,
                                if_version=op.file_version,
                            )
                        )
                    elif isinstance(op, DeleteFileOperation):
                        delete_steps.append(
                            _JournalStep(
                                kind="delete",
                                path=op.file_path,
                                if_version=op.file_version,
                                missing_ok=op.missing_ok,
                                recursive=op.recursive,
                            )
                        )

            for file_path in target_files:
                _emit_write(file_path)
            journal.extend(delete_steps)

            accepted_deleted_paths = [
                op.file_path
                for index in ordered_indices
                if outcomes.get(index) == ApplyOutcome.APPLIED
                for op in resolved_ops[index]
                if isinstance(op, DeleteFileOperation)
            ]

            if payload.dry_run:
                # ADR-0085 rule 5: a dry run never writes. Every outcome
                # decided above is a prediction of what a real run would do
                # right now.
                return ApplyCodeActionsRunResult(
                    outcomes=outcomes,
                    file_summaries={
                        file_path: FileApplySummary(written=False)
                        for file_path in target_files
                    },
                    resulting_content={
                        file_path: content
                        for file_path in target_files
                        if (content := overlay[file_path]) is not None
                    },
                    deleted_paths=accepted_deleted_paths,
                    dry_run=True,
                )

            # (7) Commit: replay the journal. Failures are recorded rather
            # than acted on immediately; whether a selection touching a failed
            # file is wholly failed or partially applied depends on steps later
            # in this same loop.
            file_summaries: dict[ResourceUri, FileApplySummary] = {}
            commit_failures: dict[ResourceUri, ApplyOutcome] = {}
            landed_files: set[ResourceUri] = set()
            deleted_paths: list[ResourceUri] = []

            for step in journal:
                path = resource_uri_to_path(step.path)
                try:
                    if step.kind == "create":
                        await session.create_file(path, "", overwrite=step.overwrite)
                        landed_files.add(step.path)
                        new_version = await session.read_file_version(path)
                        file_summaries[step.path] = FileApplySummary(
                            written=True, file_version=new_version
                        )
                    elif step.kind == "rename":
                        assert step.new_path is not None
                        await session.rename_file(
                            path,
                            resource_uri_to_path(step.new_path),
                            overwrite=step.overwrite,
                            if_version=step.if_version,
                        )
                        landed_files.add(step.path)
                        landed_files.add(step.new_path)
                        new_version = await session.read_file_version(
                            resource_uri_to_path(step.new_path)
                        )
                        file_summaries[step.new_path] = FileApplySummary(
                            written=True, file_version=new_version
                        )
                        file_summaries[step.path] = FileApplySummary(written=False)
                    elif step.kind == "write":
                        assert step.content is not None
                        await session.save_file(
                            path,
                            step.content,
                            if_version=step.if_version,
                        )
                        landed_files.add(step.path)
                        new_version = await session.read_file_version(path)
                        file_summaries[step.path] = FileApplySummary(
                            written=True, file_version=new_version
                        )
                    elif step.kind == "delete":
                        await session.delete_file(
                            path,
                            if_version=step.if_version,
                            missing_ok=step.missing_ok,
                            recursive=step.recursive,
                        )
                        landed_files.add(step.path)
                        deleted_paths.append(step.path)
                        file_summaries[step.path] = FileApplySummary(written=False)
                except ifileeditor.FileVersionConflict as conflict:
                    self.logger.warning(
                        f"Not applying code actions to {step.path}: {conflict.message}"
                    )
                    commit_failures.setdefault(step.path, ApplyOutcome.VERSION_CONFLICT)
                    file_summaries[step.path] = FileApplySummary(written=False)
                except ifileeditor.FileAlreadyExists:
                    commit_failures.setdefault(step.path, ApplyOutcome.FILE_EXISTS)
                    file_summaries[step.path] = FileApplySummary(written=False)
                except ifileeditor.FileNotFound:
                    commit_failures.setdefault(step.path, ApplyOutcome.FILE_MISSING)
                    file_summaries[step.path] = FileApplySummary(written=False)
                except Exception:
                    self.logger.exception(f"Failed to commit {step.path}")
                    commit_failures.setdefault(step.path, ApplyOutcome.WRITE_FAILED)
                    file_summaries[step.path] = FileApplySummary(written=False)

            self._retag_failed_writes(
                commit_failures, landed_files, resolved_ops, outcomes
            )

        # A file whose write failed is dropped rather than reported with the
        # content it would have had: `resulting_content` states what a file
        # NOW HAS, and after a failed save that is not knowable from here.
        return ApplyCodeActionsRunResult(
            outcomes=outcomes,
            file_summaries=file_summaries,
            resulting_content={
                file_path: content
                for file_path in target_files
                if (content := overlay[file_path]) is not None
                and file_path not in commit_failures
            },
            deleted_paths=deleted_paths,
            dry_run=False,
        )

    @staticmethod
    def _retag_failed_writes(
        commit_failures: dict[ResourceUri, ApplyOutcome],
        landed_files: set[ResourceUri],
        resolved_ops: dict[int, list[CodeActionOperation]],
        outcomes: dict[int, ApplyOutcome],
    ) -> None:
        """Re-tag every provisionally-APPLIED selection that touches a path
        whose commit failed.

        A selection can straddle the failure: some of its paths were committed
        and some were not. Reporting that as the plain failure outcome invites
        a caller to retry it, which would apply the already-committed paths a
        second time -- so it gets ``PARTIALLY_APPLIED`` instead, and only a
        selection with nothing committed at all keeps the plain failure
        outcome. A commit step that fails after an earlier irreversible step
        (a delete already landed) reaches this too, for the same reason."""
        if not commit_failures:
            return
        for index, ops in resolved_ops.items():
            if outcomes.get(index) != ApplyOutcome.APPLIED:
                continue
            touched = [path for op in ops for path in _operation_paths(op)]
            failed = [
                commit_failures[path] for path in touched if path in commit_failures
            ]
            if not failed:
                continue
            outcomes[index] = (
                ApplyOutcome.PARTIALLY_APPLIED
                if any(path in landed_files for path in touched)
                else failed[0]
            )
