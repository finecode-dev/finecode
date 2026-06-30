from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_code_hierarchy.types import SymbolKind, SymbolTag


@dataclasses.dataclass
class CallHierarchyItem:
    """A callable entity (function, method, constructor, …) identified for hierarchy navigation.

    Handlers receive this item in incoming/outgoing call requests and resolve the
    symbol from ``uri`` + ``range`` + ``selection_range`` using their language
    intelligence service. No opaque state field is needed — symbol resolution from
    source location is the handler's responsibility.
    """

    name: str
    kind: SymbolKind
    uri: ResourceUri
    """File URI where the symbol is defined."""
    range: common_types.Range
    """Full range of the symbol's definition (e.g. the whole function body)."""
    selection_range: common_types.Range
    """Range of the symbol's name token, used for cursor placement in the IDE."""
    detail: str | None = None
    """Optional human-readable detail string (e.g. a type signature or container name)."""
    tags: list[SymbolTag] | None = None


@dataclasses.dataclass
class PrepareCallHierarchyPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class PrepareCallHierarchyResult(code_action.RunActionResult):
    items: list[CallHierarchyItem] = dataclasses.field(default_factory=list)
    """Call hierarchy items at the requested position.

    Empty list means no callable entity was found at the cursor.
    Multiple items are possible when the cursor position is ambiguous.
    """

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, PrepareCallHierarchyResult):
            self.items.extend(other.items)


class TextDocumentPrepareCallHierarchyAction(code_action.Action):
    """Identify the callable entity at a document position to begin call hierarchy navigation.

    Returns zero or more ``CallHierarchyItem`` objects representing the callable
    entity under the cursor. The IDE passes these items to
    ``CallHierarchyIncomingCallsAction`` and ``CallHierarchyOutgoingCallsAction``
    to navigate the call graph. An empty result means no callable entity was
    found at the requested position.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Identify the callable entity at a document position for call hierarchy navigation."
    PAYLOAD_TYPE = PrepareCallHierarchyPayload
    RESULT_TYPE = PrepareCallHierarchyResult
