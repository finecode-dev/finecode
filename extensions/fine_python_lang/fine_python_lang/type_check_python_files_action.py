from finecode_extension_api import code_action
from fine_type_check.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunContext,
    DiagnosticFilesRunResult,
)
from fine_type_check.type_check_files_action import TypeCheckFilesAction


class TypeCheckPythonFilesAction(
    code_action.Action[
        DiagnosticFilesRunPayload,
        DiagnosticFilesRunContext,
        DiagnosticFilesRunResult,
    ]
):
    """Type-check Python source files and report type errors."""

    DESCRIPTION = "Type-check Python source files and report type errors."
    PAYLOAD_TYPE = DiagnosticFilesRunPayload
    RUN_CONTEXT_TYPE = DiagnosticFilesRunContext
    RESULT_TYPE = DiagnosticFilesRunResult
    LANGUAGE = "python"
    PARENT_ACTION = TypeCheckFilesAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
