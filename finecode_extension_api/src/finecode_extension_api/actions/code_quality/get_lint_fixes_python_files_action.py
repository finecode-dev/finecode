from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.get_lint_fixes_action import (
    GetLintFixesAction,
    GetLintFixesRunContext,
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)


class GetLintFixesPythonFilesAction(
    code_action.Action[
        GetLintFixesRunPayload,
        GetLintFixesRunContext,
        GetLintFixesRunResult,
    ]
):
    """Compute lint fixes for Python files."""

    PAYLOAD_TYPE = GetLintFixesRunPayload
    RUN_CONTEXT_TYPE = GetLintFixesRunContext
    RESULT_TYPE = GetLintFixesRunResult
    LANGUAGE = "python"
    PARENT_ACTION = GetLintFixesAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
