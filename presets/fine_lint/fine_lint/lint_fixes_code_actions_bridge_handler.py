import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri

from fine_lint.apply_code_actions_action import CodeActionOperation, TextEditOperation
from fine_lint.code_action_types import (
    CodeAction,
    DiagnosticRef,
)
from fine_lint.get_code_actions_action import (
    GetCodeActionsAction,
    GetCodeActionsRunContext,
    GetCodeActionsRunPayload,
    GetCodeActionsRunResult,
)
from fine_lint.get_lint_fixes_action import (
    GetLintFixesAction,
    GetLintFixesRunPayload,
)
from fine_lint.lint_fix import LintFix, TextEdit

PROVIDER_ID = "lint_fixes"
"""Stamped on every CodeAction this bridge builds, and the routing key a resolve
handler for this provider checks against `payload.provider` (ADR-0084)."""

# LSP code-action kind prefixes that this bridge can satisfy.
_LINT_FIX_KINDS = {"quickfix", "source.fixAll", "source.organizeImports"}


def _kind_matches(kind: str, preferred_kinds: set[str]) -> bool:
    """Return True if *kind* is, or is a sub-kind of, any kind in *preferred_kinds*.

    LSP kind matching is hierarchical: ``source.fixAll`` matches both
    ``source.fixAll`` and ``source.fixAll.ruff``.
    """
    return any(kind == k or kind.startswith(k + ".") for k in preferred_kinds)


def _collect_codes(diagnostics: list[DiagnosticRef]) -> list[str] | None:
    """Collect all codes from diagnostic refs into a flat list."""
    if not diagnostics:
        return None
    codes: list[str] = []
    for d in diagnostics:
        codes.extend(d.codes)
    return codes or None


def _refs_matching_fix(
    fix: LintFix, diagnostics: list[DiagnosticRef]
) -> list[DiagnosticRef]:
    """Return diagnostic refs that this fix addresses, matched by code."""
    if not fix.target_codes:
        # Source action (fixAll, organizeImports) — not tied to a specific diagnostic.
        return []
    return [d for d in diagnostics if any(c in d.codes for c in fix.target_codes)]


def lint_fix_operations(
    edits: dict[ResourceUri, list[TextEdit]],
    requested_file_path: ResourceUri,
    requested_file_version: str,
) -> list[CodeActionOperation]:
    """One text-edit operation per file a fix edits.

    Only the requested file's version was pinned by the run (ADR-0083 rule 3);
    any other file the fix edits has no pinned version to guard it, so it
    carries ``None`` rather than reusing the requested file's (the bug
    ADR-0083 rule 5 fixes: one version cannot speak for every edited file).
    """
    operations: list[CodeActionOperation] = [
        TextEditOperation(
            file_path=edit_file_path,
            edits=edits_for_file,
            file_version=(
                requested_file_version
                if edit_file_path == requested_file_path
                else None
            ),
        )
        for edit_file_path, edits_for_file in edits.items()
    ]
    return operations


@dataclasses.dataclass
class LintFixesCodeActionsBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class LintFixesCodeActionsBridgeHandler(
    code_action.ActionHandler[
        GetCodeActionsAction,
        LintFixesCodeActionsBridgeHandlerConfig,
    ]
):
    """Bridge handler that translates ``get_code_actions`` into ``get_lint_fixes``.

    Registered as a concurrent handler on ``GetCodeActionsAction``. Skips execution
    when the caller's ``only`` filter cannot be satisfied by lint fixes.
    """

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self.action_runner = action_runner

    async def run(
        self,
        payload: GetCodeActionsRunPayload,
        run_context: GetCodeActionsRunContext,
    ) -> GetCodeActionsRunResult:
        if payload.only is not None and not any(
            _kind_matches(k, _LINT_FIX_KINDS) for k in payload.only
        ):
            return GetCodeActionsRunResult(
                file_version=run_context.file_version,
                actions=[],
            )

        lint_fix_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(GetLintFixesAction),
            payload=GetLintFixesRunPayload(
                file_path=payload.file_path,
                range=payload.range,
                diagnostic_codes=_collect_codes(payload.diagnostics),
                kinds=payload.only,
                # The sub-action inherits the version pinned by this run's context,
                # not the caller's optional staleness guard -- run_context is the
                # source of truth once initialized (R-203).
                file_version=run_context.file_version,
            ),
            meta=run_context.meta,
        )

        actions = [
            CodeAction(
                provider=PROVIDER_ID,
                action_id=fix.fix_id,
                title=fix.title,
                kind=fix.kind,
                operations=lint_fix_operations(
                    fix.edits,
                    requested_file_path=payload.file_path,
                    requested_file_version=run_context.file_version,
                ),
                diagnostics=_refs_matching_fix(fix, payload.diagnostics),
                is_preferred=fix.is_preferred,
            )
            for fix in lint_fix_result.fixes
        ]
        return GetCodeActionsRunResult(
            file_version=lint_fix_result.file_version,
            actions=actions,
        )
