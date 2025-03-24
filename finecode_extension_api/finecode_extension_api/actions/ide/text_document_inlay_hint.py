import enum

from finecode_extension_api import code_action, common_types


class InlayHintPayload(code_action.RunActionPayload):
    text_document: common_types.TextDocumentIdentifier
    range: common_types.Range


class InlayHintKind(enum.IntEnum):
    TYPE = 1
    PARAM = 2


class InlayHint(code_action.BaseModel):
    position: common_types.Position
    label: str
    kind: InlayHintKind
    padding_left: bool = False
    padding_right: bool = False


class InlayHintResult(code_action.RunActionResult):
    hints: list[InlayHint] | None


type TextDocumentInlayHintAction = code_action.Action[
    InlayHintPayload, code_action.RunActionContext, InlayHintResult
]
