from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.lint_files_action import (
    LintFilesRunContext,
    LintFilesRunPayload,
    LintFilesRunResult,
)


class LintPythonFilesAction(
    code_action.Action[
        LintFilesRunPayload,
        LintFilesRunContext,
        LintFilesRunResult,
    ]
):
    """Lint Python source files and report diagnostics."""

    PAYLOAD_TYPE = LintFilesRunPayload
    RUN_CONTEXT_TYPE = LintFilesRunContext
    RESULT_TYPE = LintFilesRunResult
