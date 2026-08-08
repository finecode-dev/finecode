from __future__ import annotations

import contextlib
import dataclasses
import typing

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
    FileApplySummary,
    TextEditOperation,
)
from fine_lint.lint_fix import TextEdit
from fine_lint.resolve_code_action_action import (
    ResolveCodeActionAction,
    ResolveCodeActionRunPayload,
)

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="ApplyCodeActionsHandler")


@dataclasses.dataclass
class ApplyCodeActionsHandlerConfig(code_action.ActionHandlerConfig): ...


class ApplyCodeActionsHandler(
    code_action.ActionHandler[ApplyCodeActionsAction, ApplyCodeActionsHandlerConfig]
):
    """The sole writer for code-action edits.

    Runs the whole batch through, in order: resolve any stubs, refuse any
    selection resolving to an unsupported operation (design note D11), claim
    every target file (sorted, to avoid deadlocking against a concurrent
    apply over an overlapping file set) -- or merely read them for a dry run
    (design note D12) -- validate the whole batch before writing anything
    (design note D5), accept edits greedily per design notes D2-D4, and
    write (or, for a dry run, only compute the result).
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
        resolved_ops: dict[int, list[TextEditOperation]] = {}

        # (1) Resolve stubs. A selection whose provider claims nothing becomes
        # UNRESOLVED and simply does not participate further -- it must not
        # block the rest of the batch.
        for index, selection in enumerate(selections):
            if selection.operations is not None:
                operations: list[CodeActionOperation] | None = selection.operations
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
                operations = resolve_result.operations
                if operations is None:
                    outcomes[index] = ApplyOutcome.UNRESOLVED
                    continue

            # (2) Only TextEditOperation is executed. A selection resolving to
            # any other kind is refused wholesale -- none of its operations
            # are performed, since a partially-performed selection is worse
            # than a refused one (design note D11).
            if any(not isinstance(op, TextEditOperation) for op in operations):
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

            resolved_ops[index] = typing.cast(list[TextEditOperation], operations)

        target_files = sorted(
            {op.file_path for ops in resolved_ops.values() for op in ops},
            key=resource_uri_to_path,
        )
        if not target_files:
            return ApplyCodeActionsRunResult(
                outcomes=outcomes, file_summaries={}, dry_run=payload.dry_run
            )

        # (3) Claim every target file, in sorted path order -- required so two
        # concurrent applies over overlapping file sets cannot deadlock
        # against each other by claiming in different orders. A dry run only
        # reads: it must never hold an exclusive claim just to compute a
        # preview (design note D12).
        async with (
            self.file_editor.session(_FILE_OPERATION_AUTHOR) as session,
            contextlib.AsyncExitStack() as stack,
        ):
            claimed: dict[ResourceUri, ifileeditor.FileInfo] = {}
            for file_path in target_files:
                if payload.dry_run:
                    claimed[file_path] = await stack.enter_async_context(
                        session.read_file(resource_uri_to_path(file_path))
                    )
                else:
                    claimed[file_path] = await stack.enter_async_context(
                        session.modify_file(resource_uri_to_path(file_path))
                    )

            # (4) Validate the whole batch before any write. If any file
            # fails, nothing is written -- not even a file that itself
            # validated cleanly (design note D5). Every operation naming a
            # file is checked here, whether it is the first operation
            # touching that file within its selection or a later, chained
            # one (design note D11) -- ranges are always checked against
            # the file's claimed base content, exactly like the first
            # operation, since that is the only content available before
            # any greedy acceptance has been decided.
            failed_files: dict[ResourceUri, ApplyOutcome] = {}
            for file_path in target_files:
                touching = [
                    (index, op)
                    for index, ops in resolved_ops.items()
                    if index not in outcomes
                    for op in ops
                    if op.file_path == file_path
                ]
                versions = {
                    op.file_version
                    for _, op in touching
                    if op.file_version is not None
                }
                if len(versions) > 1:
                    failed_files[file_path] = ApplyOutcome.VERSION_CONFLICT
                    continue

                agreed_version = next(iter(versions), None)
                if (
                    agreed_version is not None
                    and agreed_version != claimed[file_path].version
                ):
                    failed_files[file_path] = ApplyOutcome.VERSION_CONFLICT
                    continue

                if any(
                    not _text_edit_algebra.is_valid_edit_range(
                        edit.range, claimed[file_path].content
                    )
                    for _, op in touching
                    for edit in op.edits
                ):
                    failed_files[file_path] = ApplyOutcome.INVALID_RANGE

            if failed_files:
                for file_path, outcome in failed_files.items():
                    for index, ops in resolved_ops.items():
                        if index in outcomes:
                            continue
                        if any(op.file_path == file_path for op in ops):
                            outcomes[index] = outcome
                # Every other still-undecided selection was valid on its
                # own but the batch as a whole was refused -- a re-run,
                # once the offending file is fixed, will likely apply it.
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

            # (5) Select greedily: caller order, is_preferred first as a
            # tiebreak (stable sort preserves relative order within each
            # group). An edit overlapping an already-accepted one defers
            # the whole selection rather than failing the file. Only each
            # selection's *first* operation for a given file competes for
            # that file's simultaneous batch (design note D2); any later
            # operation the same selection has for that same file is
            # chained after the batch's result instead, never merged into
            # it (design note D11).
            accepted_by_file: dict[ResourceUri, list[TextEdit]] = {
                file_path: [] for file_path in target_files
            }
            chained_ops_by_index: dict[
                int, dict[ResourceUri, list[TextEditOperation]]
            ] = {}
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
                for op in resolved_ops[index]:
                    if op.file_path not in first_op_by_file:
                        first_op_by_file[op.file_path] = op
                    else:
                        chained_by_file.setdefault(op.file_path, []).append(op)

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
                outcomes[index] = ApplyOutcome.APPLIED

            # (6) Compute each file's resulting content in memory: the
            # simultaneous batch first, then any chained operations
            # applied in turn against the content the previous one
            # produced (design note D11), in the same order selections
            # were accepted above.
            new_content: dict[ResourceUri, str] = {}
            for file_path in target_files:
                accepted_edits = accepted_by_file.get(file_path, [])
                if not accepted_edits:
                    new_content[file_path] = claimed[file_path].content
                    continue
                content = _text_edit_algebra.apply_edits(
                    claimed[file_path].content, accepted_edits
                )
                for index in ordered_indices:
                    if outcomes.get(index) != ApplyOutcome.APPLIED:
                        continue
                    for chained_op in chained_ops_by_index.get(index, {}).get(
                        file_path, []
                    ):
                        content = _text_edit_algebra.apply_edits(
                            content, chained_op.edits
                        )
                new_content[file_path] = content

            if payload.dry_run:
                # Design note D12: a dry run never writes. Every outcome
                # decided above is a prediction of what a real run would
                # do right now.
                return ApplyCodeActionsRunResult(
                    outcomes=outcomes,
                    file_summaries={
                        file_path: FileApplySummary(written=False)
                        for file_path in target_files
                    },
                    resulting_content=new_content,
                    dry_run=True,
                )

            # (7) Write. Failures are recorded rather than acted on
            # immediately: whether a selection touching a failed file is
            # wholly failed or partially applied depends on files written
            # later in this same loop, so the re-tagging waits until the
            # whole set is known.
            file_summaries: dict[ResourceUri, FileApplySummary] = {}
            write_failures: dict[ResourceUri, ApplyOutcome] = {}
            written_files: set[ResourceUri] = set()
            for file_path in target_files:
                accepted_edits = accepted_by_file.get(file_path, [])
                if not accepted_edits:
                    file_summaries[file_path] = FileApplySummary(written=False)
                    continue

                try:
                    await session.save_file(
                        file_path=resource_uri_to_path(file_path),
                        file_content=new_content[file_path],
                        if_version=claimed[file_path].version,
                    )
                except ifileeditor.FileVersionConflict as conflict:
                    self.logger.warning(
                        f"Not applying code actions to {file_path}: "
                        f"{conflict.message}"
                    )
                    write_failures[file_path] = ApplyOutcome.VERSION_CONFLICT
                    file_summaries[file_path] = FileApplySummary(written=False)
                    continue
                except Exception:
                    # logger.exception already carries the traceback; naming the
                    # file is what the message has to add.
                    self.logger.exception(f"Failed to write {file_path}")
                    write_failures[file_path] = ApplyOutcome.WRITE_FAILED
                    file_summaries[file_path] = FileApplySummary(written=False)
                    continue

                written_files.add(file_path)
                new_version = await session.read_file_version(
                    resource_uri_to_path(file_path)
                )
                file_summaries[file_path] = FileApplySummary(
                    written=True, file_version=new_version
                )

            self._retag_failed_writes(
                write_failures, written_files, resolved_ops, outcomes
            )

        # A file whose write failed is dropped rather than reported with the
        # content it would have had: `resulting_content` states what a file
        # NOW HAS, and after a failed save that is not knowable from here.
        return ApplyCodeActionsRunResult(
            outcomes=outcomes,
            file_summaries=file_summaries,
            resulting_content={
                file_path: content
                for file_path, content in new_content.items()
                if file_path not in write_failures
            },
            dry_run=False,
        )

    @staticmethod
    def _retag_failed_writes(
        write_failures: dict[ResourceUri, ApplyOutcome],
        written_files: set[ResourceUri],
        resolved_ops: dict[int, list[TextEditOperation]],
        outcomes: dict[int, ApplyOutcome],
    ) -> None:
        """Re-tag every provisionally-APPLIED selection that touches a file
        whose write failed.

        A selection editing several files can straddle the failure: some of its
        files were written and some were not. Reporting that as WRITE_FAILED
        invites a caller to retry it, which would apply the written files'
        edits a second time -- so it gets ``PARTIALLY_APPLIED`` instead, and
        only a selection with nothing written at all keeps the plain failure
        outcome."""
        if not write_failures:
            return
        for index, ops in resolved_ops.items():
            if outcomes.get(index) != ApplyOutcome.APPLIED:
                continue
            touched = [op.file_path for op in ops]
            failed = [
                write_failures[file_path]
                for file_path in touched
                if file_path in write_failures
            ]
            if not failed:
                continue
            outcomes[index] = (
                ApplyOutcome.PARTIALLY_APPLIED
                if any(file_path in written_files for file_path in touched)
                else failed[0]
            )
