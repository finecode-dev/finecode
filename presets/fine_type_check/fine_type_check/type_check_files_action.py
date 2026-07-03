# docs: docs/reference/actions.md
from finecode_extension_api import code_action
from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunContext,
    DiagnosticFilesRunResult,
)


class TypeCheckFilesAction(
    code_action.Action[
        DiagnosticFilesRunPayload,
        DiagnosticFilesRunContext,
        DiagnosticFilesRunResult,
    ]
):
    """Type-check a specific set of files and report type errors. Internal action dispatched by type_check.

    Contract: handlers (and dispatch handlers) must include every file from
    ``payload.file_paths`` in the result ``messages`` dict — use an empty list
    for files with no issues.  Omitting a file leaves stale IDE diagnostics
    visible for it.
    """

    DESCRIPTION = "Type-check specific files and report type errors. Internal action dispatched by type_check."
    PAYLOAD_TYPE = DiagnosticFilesRunPayload
    RUN_CONTEXT_TYPE = DiagnosticFilesRunContext
    RESULT_TYPE = DiagnosticFilesRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
