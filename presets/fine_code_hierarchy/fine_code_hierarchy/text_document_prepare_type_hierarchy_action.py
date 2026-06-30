from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_code_hierarchy.types import SymbolKind, SymbolTag


@dataclasses.dataclass
class TypeHierarchyItem:
    """A type entity (class, interface, struct, …) identified for hierarchy navigation.

    Handlers receive this item in supertypes/subtypes requests and resolve the
    type from ``uri`` + ``range`` + ``selection_range`` using their language
    intelligence service. No opaque state field is needed — symbol resolution from
    source location is the handler's responsibility.
    """

    name: str
    kind: SymbolKind
    uri: ResourceUri
    """File URI where the type is defined."""
    range: common_types.Range
    """Full range of the type's definition (e.g. the whole class body)."""
    selection_range: common_types.Range
    """Range of the type's name token, used for cursor placement in the IDE."""
    detail: str | None = None
    """Optional human-readable detail string (e.g. a module or namespace qualifier)."""
    tags: list[SymbolTag] | None = None


@dataclasses.dataclass
class PrepareTypeHierarchyPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class PrepareTypeHierarchyResult(code_action.RunActionResult):
    items: list[TypeHierarchyItem] = dataclasses.field(default_factory=list)
    """Type hierarchy items at the requested position.

    Empty list means no type entity was found at the cursor.
    Multiple items are possible when the cursor position is ambiguous.
    """

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, PrepareTypeHierarchyResult):
            self.items.extend(other.items)


class TextDocumentPrepareTypeHierarchyAction(code_action.Action):
    """Identify the type entity at a document position to begin type hierarchy navigation.

    Returns zero or more ``TypeHierarchyItem`` objects representing the type entity
    under the cursor. The IDE passes these items to ``TypeHierarchySupertypesAction``
    and ``TypeHierarchySubtypesAction`` to navigate the inheritance hierarchy. An
    empty result means no type entity was found at the requested position.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Identify the type entity at a document position for type hierarchy navigation."
    PAYLOAD_TYPE = PrepareTypeHierarchyPayload
    RESULT_TYPE = PrepareTypeHierarchyResult
