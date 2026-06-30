from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri


class DocumentHighlightKind(enum.IntEnum):
    TEXT = 1
    READ = 2
    WRITE = 3


@dataclasses.dataclass
class DocumentHighlight:
    range: common_types.Range
    kind: DocumentHighlightKind = DocumentHighlightKind.TEXT


@dataclasses.dataclass
class DocumentHighlightPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class DocumentHighlightResult(code_action.RunActionResult):
    highlights: list[DocumentHighlight] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, DocumentHighlightResult):
            self.highlights.extend(other.highlights)


class TextDocumentDocumentHighlightAction(code_action.Action):
    DESCRIPTION = "Find all document highlight ranges for the symbol at a document position."
    PAYLOAD_TYPE = DocumentHighlightPayload
    RESULT_TYPE = DocumentHighlightResult
    # SEQUENTIAL (default)
