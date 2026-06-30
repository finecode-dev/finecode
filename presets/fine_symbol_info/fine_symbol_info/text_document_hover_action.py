from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri


class MarkupKind(enum.Enum):
    PLAINTEXT = "plaintext"
    MARKDOWN = "markdown"


@dataclasses.dataclass
class MarkupContent:
    kind: MarkupKind
    value: str


@dataclasses.dataclass
class HoverPayload(code_action.RunActionPayload):
    uri: ResourceUri
    position: common_types.Position


@dataclasses.dataclass
class HoverResult(code_action.RunActionResult):
    content: MarkupContent | None = None
    range: common_types.Range | None = None
    """Source range that triggered the hover; used by the IDE for highlighting.
    None when the handler does not report a trigger range."""

    def update(self, other: code_action.RunActionResult) -> None:
        # First non-None content wins; later handlers do not override.
        if isinstance(other, HoverResult) and self.content is None:
            self.content = other.content
            self.range = other.range


class TextDocumentHoverAction(code_action.Action):
    DESCRIPTION = "Return hover documentation for the symbol at a document position."
    PAYLOAD_TYPE = HoverPayload
    RESULT_TYPE = HoverResult
    # SEQUENTIAL (default) — hover is single-source navigation
