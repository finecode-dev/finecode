from finecode_extension_api import code_action
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    TextDocumentPrepareTypeHierarchyAction,
    PrepareTypeHierarchyPayload,
    PrepareTypeHierarchyResult,
)


class TextDocumentPrepareTypeHierarchyPythonAction(code_action.Action):
    """Identify the type entity at a Python document position for type hierarchy navigation."""

    DESCRIPTION = "Identify the type entity at a Python document position for type hierarchy navigation."
    PAYLOAD_TYPE = PrepareTypeHierarchyPayload
    RESULT_TYPE = PrepareTypeHierarchyResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentPrepareTypeHierarchyAction
