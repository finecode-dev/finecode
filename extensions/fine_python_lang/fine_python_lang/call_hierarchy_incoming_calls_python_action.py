from finecode_extension_api import code_action
from fine_code_hierarchy.call_hierarchy_incoming_calls_action import (
    CallHierarchyIncomingCallsAction,
    CallHierarchyIncomingCallsPayload,
    CallHierarchyIncomingCallsResult,
)


class CallHierarchyIncomingCallsPythonAction(code_action.Action):
    """Return all incoming calls for a Python call hierarchy item."""

    DESCRIPTION = "Return all incoming calls for a Python call hierarchy item."
    PAYLOAD_TYPE = CallHierarchyIncomingCallsPayload
    RESULT_TYPE = CallHierarchyIncomingCallsResult
    LANGUAGE = "python"
    PARENT_ACTION = CallHierarchyIncomingCallsAction
