import dataclasses
import enum

from finecode_extension_api import code_action, common_types
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class InlayHintPayload(code_action.RunActionPayload):
    uri: ResourceUri
    range: common_types.Range


class InlayHintKind(enum.IntEnum):
    TYPE = 1
    PARAM = 2


@dataclasses.dataclass
class InlayHint:
    position: common_types.Position
    label: str
    kind: InlayHintKind
    padding_left: bool = False
    padding_right: bool = False


@dataclasses.dataclass
class InlayHintResult(code_action.RunActionResult):
    hints: list[InlayHint] = dataclasses.field(default_factory=list)
    """Inlay hints for the requested range. Empty list means the handler
    ran and found no hints."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, InlayHintResult):
            return
        self.hints.extend(other.hints)


class TextDocumentInlayHintAction(code_action.Action):
    DESCRIPTION = "Provide inlay hints for a text document."
    PAYLOAD_TYPE = InlayHintPayload
    RESULT_TYPE = InlayHintResult
