import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner

from fine_lint.apply_code_actions_action import CodeActionOperation
from fine_lint.get_lint_fixes_action import (
    GetLintFixesAction,
    GetLintFixesRunPayload,
)
from fine_lint.lint_fixes_code_actions_bridge_handler import (
    PROVIDER_ID,
    lint_fix_operations,
)
from fine_lint.resolve_code_action_action import (
    ResolveCodeActionAction,
    ResolveCodeActionRunContext,
    ResolveCodeActionRunPayload,
    ResolveCodeActionRunResult,
)


@dataclasses.dataclass
class LintFixesResolveBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class LintFixesResolveBridgeHandler(
    code_action.ActionHandler[
        ResolveCodeActionAction,
        LintFixesResolveBridgeHandlerConfig,
    ]
):
    """Bridge handler that resolves ``lint_fixes`` code actions to their edits.

    Registered as a concurrent handler on ``ResolveCodeActionAction``. Claims only
    selections whose ``provider`` is ``PROVIDER_ID`` -- the same constant
    ``LintFixesCodeActionsBridgeHandler`` stamps on the actions it builds (design
    note D1) -- and leaves every other selection to whichever handler owns it.
    """

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self.action_runner = action_runner

    async def run(
        self,
        payload: ResolveCodeActionRunPayload,
        run_context: ResolveCodeActionRunContext,
    ) -> ResolveCodeActionRunResult:
        if payload.provider != PROVIDER_ID:
            return ResolveCodeActionRunResult(
                file_version=run_context.file_version,
                operations=None,
            )

        lint_fix_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(GetLintFixesAction),
            payload=GetLintFixesRunPayload(
                file_path=payload.file_path,
                # The payload carries no range/diagnostic_codes/kinds to narrow
                # by -- action_id's shape is a handler-internal convention, not a
                # bridge contract (ADR-0084), so it is not parsed here.
                # Recomputing the whole file's fixes is the only narrowing that
                # cannot risk missing the fix.
                file_version=run_context.file_version,
            ),
            meta=run_context.meta,
        )

        matching_fix = next(
            (fix for fix in lint_fix_result.fixes if fix.fix_id == payload.action_id),
            None,
        )
        operations: list[CodeActionOperation] | None = None
        if matching_fix is not None:
            operations = lint_fix_operations(
                matching_fix.edits,
                requested_file_path=payload.file_path,
                requested_file_version=run_context.file_version,
            )
        return ResolveCodeActionRunResult(
            file_version=lint_fix_result.file_version,
            operations=operations,
        )
