from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri
from fine_symbol_info.types import Location


@dataclasses.dataclass
class DefinitionPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class DefinitionResult(code_action.RunActionResult):
    locations: list[Location] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, DefinitionResult):
            self.locations.extend(other.locations)


class TextDocumentDefinitionAction(code_action.Action):
    DESCRIPTION = "Find the definition location(s) of the symbol at a document position."
    PAYLOAD_TYPE = DefinitionPayload
    RESULT_TYPE = DefinitionResult
    # SEQUENTIAL (default) — definition is single-source navigation
