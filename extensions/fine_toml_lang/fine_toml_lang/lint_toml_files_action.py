from fine_lint.diagnostic_types import (
    DiagnosticFilesRunContext,
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunResult,
)
from fine_lint.lint_files_action import LintFilesAction
from finecode_extension_api import code_action


class LintTomlFilesAction(
    code_action.Action[
        DiagnosticFilesRunPayload,
        DiagnosticFilesRunContext,
        DiagnosticFilesRunResult,
    ]
):
    """Lint TOML files and report diagnostics."""

    PAYLOAD_TYPE = DiagnosticFilesRunPayload
    RUN_CONTEXT_TYPE = DiagnosticFilesRunContext
    RESULT_TYPE = DiagnosticFilesRunResult
    LANGUAGE = "toml"
    PARENT_ACTION = LintFilesAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
