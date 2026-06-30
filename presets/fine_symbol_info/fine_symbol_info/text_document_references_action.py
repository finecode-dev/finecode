from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_symbol_info.types import Location


@dataclasses.dataclass
class ReferencesPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position
    include_declaration: bool = True
    """Whether to include the symbol's own declaration site in the results."""


@dataclasses.dataclass
class ReferencesResult(code_action.RunActionResult):
    locations: list[Location] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ReferencesResult):
            self.locations.extend(other.locations)


class TextDocumentReferencesAction(code_action.Action):
    DESCRIPTION = "Find all reference locations of the symbol at a document position."
    PAYLOAD_TYPE = ReferencesPayload
    RESULT_TYPE = ReferencesResult
    # SEQUENTIAL (default)
