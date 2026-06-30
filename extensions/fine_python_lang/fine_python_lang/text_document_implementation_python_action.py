from finecode_extension_api import code_action
from fine_symbol_info.text_document_implementation_action import (
    ImplementationPayload,
    ImplementationResult,
    TextDocumentImplementationAction,
)


class TextDocumentImplementationPythonAction(code_action.Action):
    """Find the implementation location(s) of the symbol at a Python document position."""

    DESCRIPTION = "Find the implementation location(s) of the symbol at a Python document position."
    PAYLOAD_TYPE = ImplementationPayload
    RESULT_TYPE = ImplementationResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentImplementationAction
