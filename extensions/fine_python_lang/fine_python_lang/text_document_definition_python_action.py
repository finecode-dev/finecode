from finecode_extension_api import code_action
from fine_symbol_info.text_document_definition_action import (
    DefinitionPayload,
    DefinitionResult,
    TextDocumentDefinitionAction,
)


class TextDocumentDefinitionPythonAction(code_action.Action):
    """Find the definition location(s) of the symbol at a Python document position."""

    DESCRIPTION = "Find the definition location(s) of the symbol at a Python document position."
    PAYLOAD_TYPE = DefinitionPayload
    RESULT_TYPE = DefinitionResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentDefinitionAction
