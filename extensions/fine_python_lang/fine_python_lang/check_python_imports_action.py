# docs: docs/reference/actions.md
from finecode_extension_api import code_action
from fine_check_imports.check_imports_action import (
    CheckImportsAction,
    CheckImportsRunPayload,
    CheckImportsRunResult,
    CheckImportsRunContext,
)


class CheckPythonImportsAction(
    code_action.Action[
        CheckImportsRunPayload,
        CheckImportsRunContext,
        CheckImportsRunResult,
    ]
):
    """Check Python import-graph contracts (e.g. import-linter) and report diagnostics."""

    DESCRIPTION = "Check Python import-graph contracts and report diagnostics."
    PAYLOAD_TYPE = CheckImportsRunPayload
    RUN_CONTEXT_TYPE = CheckImportsRunContext
    RESULT_TYPE = CheckImportsRunResult
    LANGUAGE = "python"
    PARENT_ACTION = CheckImportsAction
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
