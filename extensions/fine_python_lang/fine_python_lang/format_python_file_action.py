from finecode_extension_api import code_action
from fine_format.format_file_action import (
    FormatFileAction,
    FormatFileRunContext,
    FormatFileRunPayload,
    FormatFileRunResult,
)


class FormatPythonFileAction(
    code_action.Action[
        FormatFileRunPayload,
        FormatFileRunContext,
        FormatFileRunResult,
    ]
):
    """Format a single Python file. Item-level action; handlers run sequentially (pipeline)."""

    DESCRIPTION = "Format a single Python file. Item-level action; handlers run sequentially (pipeline mode)."
    PAYLOAD_TYPE = FormatFileRunPayload
    RUN_CONTEXT_TYPE = FormatFileRunContext
    RESULT_TYPE = FormatFileRunResult
    LANGUAGE = "python"
    PARENT_ACTION = FormatFileAction
