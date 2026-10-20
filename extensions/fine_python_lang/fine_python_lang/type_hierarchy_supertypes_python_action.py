from fine_code_hierarchy.type_hierarchy_supertypes_action import (
    TypeHierarchySupertypesAction,
    TypeHierarchySupertypesPayload,
    TypeHierarchySupertypesResult,
)
from finecode_extension_api import code_action


class TypeHierarchySupertypesPythonAction(code_action.Action):
    """Return the direct supertypes of a Python type hierarchy item."""

    DESCRIPTION = "Return the direct supertypes of a Python type hierarchy item."
    PAYLOAD_TYPE = TypeHierarchySupertypesPayload
    RESULT_TYPE = TypeHierarchySupertypesResult
    LANGUAGE = "python"
    PARENT_ACTION = TypeHierarchySupertypesAction
