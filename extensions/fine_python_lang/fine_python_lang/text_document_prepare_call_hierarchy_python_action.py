from finecode_extension_api import code_action
from fine_code_hierarchy.text_document_prepare_call_hierarchy_action import (
    TextDocumentPrepareCallHierarchyAction,
    PrepareCallHierarchyPayload,
    PrepareCallHierarchyResult,
)


class TextDocumentPrepareCallHierarchyPythonAction(code_action.Action):
    """Identify the callable entity at a Python document position for call hierarchy navigation."""

    DESCRIPTION = "Identify the callable entity at a Python document position for call hierarchy navigation."
    PAYLOAD_TYPE = PrepareCallHierarchyPayload
    RESULT_TYPE = PrepareCallHierarchyResult
    LANGUAGE = "python"
    PARENT_ACTION = TextDocumentPrepareCallHierarchyAction
