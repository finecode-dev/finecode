# docs: docs/reference/actions.md
from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import (
    Position,
    Range,
    DiagnosticSeverity as LintMessageSeverity,
    Diagnostic as LintMessage,
    DiagnosticFilesRunPayload as LintFilesRunPayload,
    DiagnosticFilesRunResult as LintFilesRunResult,
    DiagnosticFilesRunContext as LintFilesRunContext,
)


class LintFilesAction(
    code_action.Action[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    """Run linters on specific files and report diagnostics. Internal action dispatched by lint.

    Contract: handlers (and dispatch handlers) must include every file from
    ``payload.file_paths`` in the result ``messages`` dict — use an empty list
    for files with no issues.  Omitting a file leaves stale IDE diagnostics
    visible for it.
    """

    DESCRIPTION = "Run linters on specific files and report diagnostics. Internal action dispatched by lint."
    PAYLOAD_TYPE = LintFilesRunPayload
    RUN_CONTEXT_TYPE = LintFilesRunContext
    RESULT_TYPE = LintFilesRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
