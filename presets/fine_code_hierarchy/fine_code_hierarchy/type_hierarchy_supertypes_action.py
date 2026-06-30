from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    TypeHierarchyItem,
)


@dataclasses.dataclass
class TypeHierarchySupertypesPayload(code_action.RunActionPayload):
    item: TypeHierarchyItem
    """The type item whose supertypes (base classes, parent interfaces) should be returned."""


@dataclasses.dataclass
class TypeHierarchySupertypesResult(code_action.RunActionResult):
    items: list[TypeHierarchyItem] = dataclasses.field(default_factory=list)
    """Direct supertypes of the queried item. Empty means no supertypes were found."""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, TypeHierarchySupertypesResult):
            self.items.extend(other.items)


class TypeHierarchySupertypesAction(code_action.Action):
    """Return the direct supertypes (base classes, parent interfaces) of a type hierarchy item.

    An empty result means the queried type has no supertypes or they could not be resolved.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Return the direct supertypes of a type hierarchy item."
    PAYLOAD_TYPE = TypeHierarchySupertypesPayload
    RESULT_TYPE = TypeHierarchySupertypesResult
