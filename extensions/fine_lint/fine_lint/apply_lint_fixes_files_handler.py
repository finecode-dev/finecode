from __future__ import annotations

import dataclasses
import hashlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ifileeditor,
    ilogger,
    iprojectactionrunner,
    iuser_messenger,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_lint.apply_code_actions_action import (
    ApplyCodeActionsAction,
    ApplyCodeActionsRunPayload,
    ApplyOutcome,
    CodeActionSelection,
    TextEditOperation,
)
from fine_lint.apply_lint_fixes_files_action import (
    ApplyLintFixesFilesAction,
    ApplyLintFixesFilesRunContext,
    ApplyLintFixesFilesRunPayload,
    ApplyLintFixesFilesRunResult,
    ConvergenceStatus,
)
from fine_lint.get_lint_fixes_action import GetLintFixesAction, GetLintFixesRunPayload
from fine_lint.lint_fix import FixApplicability, LintFix
from fine_lint.lint_fixes_code_actions_bridge_handler import PROVIDER_ID, _kind_matches

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
    id="ApplyLintFixesFilesHandler"
)


def _merge_fixes(
    *sources: dict[ResourceUri, list[LintFix]],
) -> dict[ResourceUri, list[LintFix]]:
    """Concatenate several per-file fix maps into one."""
    merged: dict[ResourceUri, list[LintFix]] = {}
    for source in sources:
        for file_path, fixes in source.items():
            merged.setdefault(file_path, []).extend(fixes)
    return merged


@dataclasses.dataclass
class ApplyLintFixesFilesHandlerConfig(code_action.ActionHandlerConfig): ...


class ApplyLintFixesFilesHandler(
    code_action.ActionHandler[
        ApplyLintFixesFilesAction, ApplyLintFixesFilesHandlerConfig
    ]
):
    """Owns the re-fix pass loop (ADR-0085).

    Each pass re-requests fixes against the files' latest content, filters by
    applicability and kind (ADR-0085 rule 4), and applies the survivors as one
    ``apply_code_actions`` batch -- re-deriving positions for free and dropping
    fixes another provider's edits invalidated (ADR-0083, semantic interference). Stops
    on the first of: a pass that applies nothing (``CONVERGED``), a pass whose
    resulting content for some file was already seen in an earlier pass
    (``OSCILLATED`` -- two fixes undoing each other), or ``max_passes``
    (``MAX_PASSES_REACHED``).
    """

    def __init__(
        self,
        file_editor: ifileeditor.IFileEditor,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
        user_messenger: iuser_messenger.IUserMessenger,
    ) -> None:
        self.file_editor = file_editor
        self.action_runner = action_runner
        self.logger = logger
        self.user_messenger = user_messenger

    @staticmethod
    def _is_applicable(fix: LintFix, include_unsafe: bool) -> bool:
        if fix.applicability == FixApplicability.SAFE:
            return True
        if fix.applicability == FixApplicability.UNSAFE:
            return include_unsafe
        return False  # DISPLAY_ONLY -- never applied (ADR-0085 rule 4)

    async def _content_hashes(
        self, file_paths: list[ResourceUri]
    ) -> dict[ResourceUri, str]:
        hashes: dict[ResourceUri, str] = {}
        async with self.file_editor.session(_FILE_OPERATION_AUTHOR) as session:
            for file_path in file_paths:
                async with session.read_file(resource_uri_to_path(file_path)) as info:
                    hashes[file_path] = hashlib.sha256(
                        info.content.encode("utf-8")
                    ).hexdigest()
        return hashes

    def _report_non_convergence(
        self,
        status: ConvergenceStatus,
        remaining_fixes: dict[ResourceUri, list[LintFix]],
        max_passes: int,
        run_context: ApplyLintFixesFilesRunContext,
    ) -> None:
        """Report a truncated run (ADR-0085; `applying-code-actions` Q4): a caller polling only
        the CLI/IDE result would otherwise see ``OSCILLATED`` and
        ``MAX_PASSES_REACHED`` land nowhere but the result and read a run that
        did not finish fixing the project as indistinguishable from a fully
        successful one."""
        if status == ConvergenceStatus.CONVERGED:
            return

        if status == ConvergenceStatus.OSCILLATED:
            details = "; ".join(
                f"{file_path}: {', '.join(fix.fix_id for fix in fixes)}"
                for file_path, fixes in remaining_fixes.items()
            )
            message = (
                "ApplyLintFixesFilesHandler: non-convergence -- fixes kept "
                f"undoing each other and the run was stopped early: {details}"
            )
        else:
            remaining_count = sum(len(fixes) for fixes in remaining_fixes.values())
            message = (
                "ApplyLintFixesFilesHandler: non-convergence -- exhausted the "
                f"{max_passes}-pass budget without converging; {remaining_count} "
                "fix(es) remain unapplied"
            )

        if run_context.meta.trigger == code_action.RunActionTrigger.USER:
            self.user_messenger.warning(message)
        else:
            # System-triggered calls (e.g. editor fix-on-save) should not
            # prompt a user for something that may retry automatically --
            # diagnosable in the ER logs instead (R-505's precedent).
            self.logger.debug(message)

    async def _fetch_pass_candidates(
        self,
        payload: ApplyLintFixesFilesRunPayload,
        run_context: ApplyLintFixesFilesRunContext,
    ) -> tuple[
        list[tuple[ResourceUri, LintFix]],
        dict[ResourceUri, str],
        dict[ResourceUri, list[LintFix]],
    ]:
        """Fetch and filter fixes for every requested file -- one pass' worth
        of candidates, applicability- and kind-filtered (ADR-0085 rule 4).

        Returns the candidates, the version each file's fixes were computed
        against, and the fixes held back because that version does not speak
        for all of them."""
        fixes_by_file: dict[ResourceUri, list[LintFix]] = {}
        file_versions: dict[ResourceUri, str] = {}
        diverged: dict[ResourceUri, list[LintFix]] = {}
        for file_path in payload.file_paths:
            fixes_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    GetLintFixesAction
                ),
                payload=GetLintFixesRunPayload(
                    file_path=file_path,
                    kinds=payload.kinds,
                ),
                meta=run_context.meta,
            )
            file_versions[file_path] = fixes_result.file_version
            applicable = [
                fix
                for fix in fixes_result.fixes
                if self._is_applicable(fix, payload.include_unsafe)
                and (
                    payload.kinds is None or _kind_matches(fix.kind, set(payload.kinds))
                )
            ]
            if fixes_result.version_diverged:
                # The contributions were computed against two different
                # contents, so `fixes_result.file_version` speaks for only some
                # of them. `GetLintFixesRunResult.version_diverged` says such a
                # result MUST NOT be used as an apply batch -- stamping the one
                # result-level version onto every selection would hide the mix
                # from apply's version guard too, and the batch would be applied
                # as if simultaneous. Hold them back; the next pass re-derives
                # them against whatever content the file settled on.
                if applicable:
                    diverged[file_path] = applicable
                continue
            fixes_by_file[file_path] = applicable

        candidates = [
            (file_path, fix)
            for file_path, fixes in fixes_by_file.items()
            for fix in fixes
        ]
        return candidates, file_versions, diverged

    @staticmethod
    def _selections_for(
        candidates: list[tuple[ResourceUri, LintFix]],
        file_versions: dict[ResourceUri, str],
    ) -> list[CodeActionSelection]:
        """Build one selection per candidate fix, mapping ``LintFix.edits``
        into one ``TextEditOperation`` per file. Only the file the fix was
        requested for gets the pinned version -- any other file the fix edits
        (`applying-code-actions` Q1) has no pinned version to guard it and gets None
        (this is the bug ADR-0083 rule 5 fixes: one version cannot speak for every edited
        file, and versions are content hashes that agree by coincidence for
        files with identical content)."""
        return [
            CodeActionSelection(
                provider=PROVIDER_ID,
                action_id=fix.fix_id,
                file_path=file_path,
                operations=[
                    TextEditOperation(
                        file_path=edit_file_path,
                        edits=edits_for_file,
                        file_version=(
                            file_versions[file_path]
                            if edit_file_path == file_path
                            else None
                        ),
                    )
                    for edit_file_path, edits_for_file in fix.edits.items()
                ],
                is_preferred=fix.is_preferred,
            )
            for file_path, fix in candidates
        ]

    async def _run_dry(
        self,
        payload: ApplyLintFixesFilesRunPayload,
        run_context: ApplyLintFixesFilesRunContext,
    ) -> ApplyLintFixesFilesRunResult:
        """Preview pass 1 only (ADR-0085 rule 5): later passes would need
        ``get_lint_fixes`` to run against simulated content, but providers
        read through the file editor rather than an injected string."""
        candidates, file_versions, diverged = await self._fetch_pass_candidates(
            payload, run_context
        )
        applied_counts: dict[ResourceUri, int] = dict.fromkeys(payload.file_paths, 0)
        if not candidates:
            return ApplyLintFixesFilesRunResult(
                applied_counts=applied_counts,
                remaining_fixes=diverged,
                status=ConvergenceStatus.PREVIEWED,
                passes=1,
            )

        selections = self._selections_for(candidates, file_versions)
        apply_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                ApplyCodeActionsAction
            ),
            payload=ApplyCodeActionsRunPayload(selections=selections, dry_run=True),
            meta=run_context.meta,
        )

        not_applied: dict[ResourceUri, list[LintFix]] = {}
        for index, (file_path, fix) in enumerate(candidates):
            if apply_result.outcomes.get(index) == ApplyOutcome.APPLIED:
                applied_counts[file_path] += 1
            else:
                not_applied.setdefault(file_path, []).append(fix)

        return ApplyLintFixesFilesRunResult(
            applied_counts=applied_counts,
            remaining_fixes=_merge_fixes(not_applied, diverged),
            status=ConvergenceStatus.PREVIEWED,
            passes=1,
            resulting_content=apply_result.resulting_content,
        )

    async def run(
        self,
        payload: ApplyLintFixesFilesRunPayload,
        run_context: ApplyLintFixesFilesRunContext,
    ) -> ApplyLintFixesFilesRunResult:
        if payload.dry_run:
            return await self._run_dry(payload, run_context)

        seen_hashes: dict[ResourceUri, set[str]] = {
            file_path: {digest}
            for file_path, digest in (
                await self._content_hashes(payload.file_paths)
            ).items()
        }
        applied_counts: dict[ResourceUri, int] = dict.fromkeys(payload.file_paths, 0)
        remaining_fixes: dict[ResourceUri, list[LintFix]] = {}
        status = ConvergenceStatus.CONVERGED
        passes = 0

        for pass_number in range(1, payload.max_passes + 1):
            passes = pass_number

            candidates, file_versions, diverged = await self._fetch_pass_candidates(
                payload, run_context
            )
            if not candidates:
                remaining_fixes = diverged
                if diverged and pass_number < payload.max_passes:
                    # Nothing batchable, but only because a file was observed
                    # at two versions at once. That is a race, not a fixed
                    # point -- spend another pass rather than calling it
                    # CONVERGED.
                    continue
                status = (
                    ConvergenceStatus.MAX_PASSES_REACHED
                    if diverged
                    else ConvergenceStatus.CONVERGED
                )
                break

            selections = self._selections_for(candidates, file_versions)

            apply_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    ApplyCodeActionsAction
                ),
                payload=ApplyCodeActionsRunPayload(selections=selections),
                meta=run_context.meta,
            )

            applied_this_pass = 0
            applied_fixes_this_pass: dict[ResourceUri, list[LintFix]] = {}
            remaining_this_pass: dict[ResourceUri, list[LintFix]] = {}
            for index, (file_path, fix) in enumerate(candidates):
                if apply_result.outcomes.get(index) == ApplyOutcome.APPLIED:
                    applied_counts[file_path] += 1
                    applied_this_pass += 1
                    applied_fixes_this_pass.setdefault(file_path, []).append(fix)
                else:
                    remaining_this_pass.setdefault(file_path, []).append(fix)

            if applied_this_pass == 0:
                remaining_fixes = _merge_fixes(remaining_this_pass, diverged)
                status = ConvergenceStatus.CONVERGED
                break

            touched_files = sorted(applied_fixes_this_pass)
            new_hashes = await self._content_hashes(touched_files)
            oscillated_files = [
                file_path
                for file_path, digest in new_hashes.items()
                if digest in seen_hashes.get(file_path, set())
            ]
            for file_path, digest in new_hashes.items():
                seen_hashes.setdefault(file_path, set()).add(digest)

            if oscillated_files:
                # Name the fixes involved: the ones just applied are exactly
                # what flipped the content back to a state already seen.
                remaining_fixes = {
                    file_path: applied_fixes_this_pass[file_path]
                    for file_path in oscillated_files
                }
                status = ConvergenceStatus.OSCILLATED
                break

            if pass_number == payload.max_passes:
                remaining_fixes = _merge_fixes(remaining_this_pass, diverged)
                # A budget that ran out with nothing left over is not a failed
                # run: every candidate this pass produced was applied. Calling
                # that MAX_PASSES_REACHED returns ERROR and warns the user that
                # the run "exhausted the 3-pass budget without converging; 0
                # fix(es) remain unapplied" -- after a run that did all its
                # work. Only leftovers make the exhausted budget meaningful.
                status = (
                    ConvergenceStatus.MAX_PASSES_REACHED
                    if remaining_fixes
                    else ConvergenceStatus.CONVERGED
                )
                break
            # Otherwise: progress was made and no oscillation was detected --
            # spend another pass re-deriving fixes against the new content.
        else:
            # Only reached when max_passes <= 0 -- no pass ever ran.
            status = ConvergenceStatus.MAX_PASSES_REACHED

        self._report_non_convergence(
            status, remaining_fixes, payload.max_passes, run_context
        )

        return ApplyLintFixesFilesRunResult(
            applied_counts=applied_counts,
            remaining_fixes=remaining_fixes,
            status=status,
            passes=passes,
        )
