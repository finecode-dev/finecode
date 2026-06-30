from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from fine_code_hierarchy.text_document_prepare_call_hierarchy_action import (
    CallHierarchyItem,
)


@dataclasses.dataclass
class CallHierarchyIncomingCall:
    caller: CallHierarchyItem
    """The function or method that calls the queried item."""
    call_ranges: list[common_types.Range]
    """Ranges within ``caller`` where the queried item is invoked."""


@dataclasses.dataclass
class CallHierarchyIncomingCallsPayload(code_action.RunActionPayload):
    item: CallHierarchyItem
    """The item whose callers should be returned."""


@dataclasses.dataclass
class CallHierarchyIncomingCallsResult(code_action.RunActionResult):
    calls: list[CallHierarchyIncomingCall] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, CallHierarchyIncomingCallsResult):
            self.calls.extend(other.calls)


class CallHierarchyIncomingCallsAction(code_action.Action):
    """Return all incoming calls for a call hierarchy item.

    Each result entry includes the caller and the exact ranges within that caller
    where the queried item is invoked. An empty result means no callers were found.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Return all incoming calls for a call hierarchy item."
    PAYLOAD_TYPE = CallHierarchyIncomingCallsPayload
    RESULT_TYPE = CallHierarchyIncomingCallsResult
