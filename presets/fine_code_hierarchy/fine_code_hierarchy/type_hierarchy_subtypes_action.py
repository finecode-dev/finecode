from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_code_hierarchy.text_document_prepare_type_hierarchy_action import (
    TypeHierarchyItem,
)


@dataclasses.dataclass
class TypeHierarchySubtypesPayload(code_action.RunActionPayload):
    item: TypeHierarchyItem
    """The type item whose subtypes (subclasses, implementors) should be returned."""


@dataclasses.dataclass
class TypeHierarchySubtypesResult(code_action.RunActionResult):
    items: list[TypeHierarchyItem] = dataclasses.field(default_factory=list)
    """Direct subtypes of the queried item. Empty means no subtypes were found."""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, TypeHierarchySubtypesResult):
            self.items.extend(other.items)


class TypeHierarchySubtypesAction(code_action.Action):
    """Return the direct subtypes (subclasses, implementors) of a type hierarchy item.

    An empty result means the queried type has no known subtypes or they could not
    be resolved. Results typically reflect only the source code visible to the handler.

    Use language-specific subactions to restrict handlers to a particular language.
    """

    DESCRIPTION = "Return the direct subtypes of a type hierarchy item."
    PAYLOAD_TYPE = TypeHierarchySubtypesPayload
    RESULT_TYPE = TypeHierarchySubtypesResult
