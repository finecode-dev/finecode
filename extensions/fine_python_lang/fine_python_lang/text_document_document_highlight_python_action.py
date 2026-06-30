from finecode_extension_api import code_action
from fine_symbol_info.text_document_document_highlight_action import (
    DocumentHighlightPayload,
    DocumentHighlightResult,
    TextDocumentDocumentHighlightAction,
)


class TextDocumentDocumentHighlightPythonAction(code_action.Action):
    """Find all document highlight ranges for the symbol at a Python document position."""

    DESCRIPTION = "Find all document highlight ranges for the symbol at a Python document position."
    PAYLOAD_TYPE = DocumentHighlightPayload
    RESULT_TYPE = DocumentHighlightResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentDocumentHighlightAction
