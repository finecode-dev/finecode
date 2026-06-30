from finecode_extension_api import code_action
from fine_code_hierarchy.call_hierarchy_outgoing_calls_action import (
    CallHierarchyOutgoingCallsAction,
    CallHierarchyOutgoingCallsPayload,
    CallHierarchyOutgoingCallsResult,
)


class CallHierarchyOutgoingCallsPythonAction(code_action.Action):
    """Return all outgoing calls from a Python call hierarchy item."""

    DESCRIPTION = "Return all outgoing calls from a Python call hierarchy item."
    PAYLOAD_TYPE = CallHierarchyOutgoingCallsPayload
    RESULT_TYPE = CallHierarchyOutgoingCallsResult
    LANGUAGE = "python"
    PARENT_ACTION = CallHierarchyOutgoingCallsAction
