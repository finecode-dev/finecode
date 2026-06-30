from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from fine_code_hierarchy.text_document_prepare_call_hierarchy_action import (
    CallHierarchyItem,
)


@dataclasses.dataclass
class CallHierarchyOutgoingCall:
    callee: CallHierarchyItem
    """The function or method called by the queried item."""
    call_ranges: list[common_types.Range]
    """Ranges within the queried item's body where ``callee`` is invoked."""


@dataclasses.dataclass
class CallHierarchyOutgoingCallsPayload(code_action.RunActionPayload):
    item: CallHierarchyItem
    """The item whose callees should be returned."""


@dataclasses.dataclass
class CallHierarchyOutgoingCallsResult(code_action.RunActionResult):
    calls: list[CallHierarchyOutgoingCall] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, CallHierarchyOutgoingCallsResult):
            self.calls.extend(other.calls)


class CallHierarchyOutgoingCallsAction(code_action.Action):
    """Return all outgoing calls from a call hierarchy item.

    Each result entry includes the callee and the exact ranges within the queried
    item's body where the callee is invoked. An empty result means no callees were found.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Return all outgoing calls from a call hierarchy item."
    PAYLOAD_TYPE = CallHierarchyOutgoingCallsPayload
    RESULT_TYPE = CallHierarchyOutgoingCallsResult
