from finecode_extension_api import code_action
from fine_symbol_info.text_document_type_definition_action import (
    TypeDefinitionPayload,
    TypeDefinitionResult,
    TextDocumentTypeDefinitionAction,
)


class TextDocumentTypeDefinitionPythonAction(code_action.Action):
    """Find the type definition location(s) of the symbol at a Python document position."""

    DESCRIPTION = "Find the type definition location(s) of the symbol at a Python document position."
    PAYLOAD_TYPE = TypeDefinitionPayload
    RESULT_TYPE = TypeDefinitionResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentTypeDefinitionAction
