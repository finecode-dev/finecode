from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_symbol_info.types import Location


@dataclasses.dataclass
class TypeDefinitionPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class TypeDefinitionResult(code_action.RunActionResult):
    locations: list[Location] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, TypeDefinitionResult):
            self.locations.extend(other.locations)


class TextDocumentTypeDefinitionAction(code_action.Action):
    DESCRIPTION = "Find the type definition location(s) of the symbol at a document position."
    PAYLOAD_TYPE = TypeDefinitionPayload
    RESULT_TYPE = TypeDefinitionResult
    # SEQUENTIAL (default)
