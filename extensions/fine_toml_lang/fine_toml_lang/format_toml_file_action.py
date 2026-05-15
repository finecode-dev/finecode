from finecode_extension_api import code_action
from fine_format.format_file_action import (
    FormatFileAction,
    FormatFileRunContext,
    FormatFileRunPayload,
    FormatFileRunResult,
)


class FormatTomlFileAction(
    code_action.Action[
        FormatFileRunPayload,
        FormatFileRunContext,
        FormatFileRunResult,
    ]
):
    """Format a single TOML file."""

    PAYLOAD_TYPE = FormatFileRunPayload
    RUN_CONTEXT_TYPE = FormatFileRunContext
    RESULT_TYPE = FormatFileRunResult
    LANGUAGE = "toml"
    PARENT_ACTION = FormatFileAction
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
