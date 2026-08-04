# docs: docs/reference/actions.md
from fine_inspect_code.diagnostic_types import Diagnostic as LintMessage
from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunContext as LintFilesRunContext,
)
from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunPayload as LintFilesRunPayload,
)
from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunResult as LintFilesRunResult,
)
from fine_inspect_code.diagnostic_types import DiagnosticSeverity as LintMessageSeverity
from fine_inspect_code.diagnostic_types import (
    Position,
    Range,
)
from finecode_extension_api import code_action


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
