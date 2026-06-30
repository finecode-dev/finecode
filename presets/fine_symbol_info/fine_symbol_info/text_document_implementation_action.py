from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_symbol_info.types import Location


@dataclasses.dataclass
class ImplementationPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class ImplementationResult(code_action.RunActionResult):
    locations: list[Location] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, ImplementationResult):
            self.locations.extend(other.locations)


class TextDocumentImplementationAction(code_action.Action):
    DESCRIPTION = "Find the implementation location(s) of the symbol at a document position."
    PAYLOAD_TYPE = ImplementationPayload
    RESULT_TYPE = ImplementationResult
    # SEQUENTIAL (default)
