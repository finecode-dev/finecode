import dataclasses
import enum

from finecode_extension_api import code_action, common_types


@dataclasses.dataclass
class InlayHintPayload(code_action.RunActionPayload):
    text_document: common_types.TextDocumentIdentifier
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
    hints: list[InlayHint] | None


class TextDocumentInlayHintAction(code_action.Action):
    PAYLOAD_TYPE = InlayHintPayload
    RESULT_TYPE = InlayHintResult
