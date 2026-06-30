from finecode_extension_api import code_action
from fine_symbol_info.text_document_references_action import (
    ReferencesPayload,
    ReferencesResult,
    TextDocumentReferencesAction,
)


class TextDocumentReferencesPythonAction(code_action.Action):
    """Find all reference locations of the symbol at a Python document position."""

    DESCRIPTION = "Find all reference locations of the symbol at a Python document position."
    PAYLOAD_TYPE = ReferencesPayload
    RESULT_TYPE = ReferencesResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentReferencesAction
