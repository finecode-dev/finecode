from finecode_extension_api import code_action
from fine_lint.lint_files_action import (
    LintFilesAction,
    LintFilesRunContext,
    LintFilesRunPayload,
    LintFilesRunResult,
)


class LintTomlFilesAction(
    code_action.Action[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    """Lint TOML files and report diagnostics."""

    PAYLOAD_TYPE = LintFilesRunPayload
    RUN_CONTEXT_TYPE = LintFilesRunContext
    RESULT_TYPE = LintFilesRunResult
    LANGUAGE = "toml"
    PARENT_ACTION = LintFilesAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
