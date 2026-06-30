from finecode_extension_api import code_action
from fine_code_hierarchy.type_hierarchy_subtypes_action import (
    TypeHierarchySubtypesAction,
    TypeHierarchySubtypesPayload,
    TypeHierarchySubtypesResult,
)


class TypeHierarchySubtypesPythonAction(code_action.Action):
    """Return the direct subtypes of a Python type hierarchy item."""

    DESCRIPTION = "Return the direct subtypes of a Python type hierarchy item."
    PAYLOAD_TYPE = TypeHierarchySubtypesPayload
    RESULT_TYPE = TypeHierarchySubtypesResult
    LANGUAGE = "python"
    PARENT_ACTION = TypeHierarchySubtypesAction
