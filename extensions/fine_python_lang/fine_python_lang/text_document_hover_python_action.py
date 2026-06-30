from finecode_extension_api import code_action
from fine_symbol_info.text_document_hover_action import (
    HoverPayload,
    HoverResult,
    TextDocumentHoverAction,
)


class TextDocumentHoverPythonAction(code_action.Action):
    """Return hover documentation for the symbol at a Python document position."""

    DESCRIPTION = "Return hover documentation for the symbol at a Python document position."
    PAYLOAD_TYPE = HoverPayload
    RESULT_TYPE = HoverResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentHoverAction
