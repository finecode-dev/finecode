from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.format_files_action import (
    FormatFilesAction,
    FormatFilesRunContext,
    FormatFilesRunPayload,
    FormatFilesRunResult,
)


class FormatPythonFilesAction(
    code_action.Action[
        FormatFilesRunPayload,
        FormatFilesRunContext,
        FormatFilesRunResult,
    ]
):
    """Format Python source files."""

    PAYLOAD_TYPE = FormatFilesRunPayload
    RUN_CONTEXT_TYPE = FormatFilesRunContext
    RESULT_TYPE = FormatFilesRunResult
    LANGUAGE = "python"
    PARENT_ACTION = FormatFilesAction
