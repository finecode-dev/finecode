# TODO: some fields are missing
from __future__ import annotations
import enum

from finecode_extension_api.code_action import BaseModel, RunActionPayload, RunActionResult


class Position(BaseModel):
    line: int
    character: int


class Range(BaseModel):
    start: Position
    end: Position


class TextDocumentIdentifier(BaseModel):
    uri: str


class TextDocumentItem(BaseModel):
    uri: str
    language_id: str
    version: int
    text: str


class InlayHintPayload(RunActionPayload):
    text_document: TextDocumentIdentifier
    range: Range


class InlayHintKind(enum.IntEnum):
    TYPE = 1
    PARAM = 2


class InlayHint(BaseModel):
    position: Position
    label: str
    kind: InlayHintKind
    padding_left: bool = False
    padding_right: bool = False


class InlayHintResult(RunActionResult):
    hints: list[InlayHint] | None


class CodeActionPayload(RunActionPayload):
    text_document: TextDocumentIdentifier
    range: Range


class CodeActionKind(enum.Enum):
    EMPTY = ""
    QUICK_FIX = "quickfix"
    REFACTOR = "refactor"
    REFACTOR_EXTRACT = "refactor.extract"
    REFACTOR_INLINE = "refactor.inline"
    REFACTOR_MOVE = "refactor.move"
    REFACTOR_REWRITE = "refactor.rewrite"
    SOURCE = "source"
    SOURCE_ORGANIZE_IMPORTS = "source.organizeImports"
    SOURCE_FIX_ALL = "source.fixAll"
    NOTEBOOK = "notebook"


class CodeActionTriggerKind(enum.IntEnum):
    INVOKED = 1
    AUTOMATIC = 2


class CodeActionContext(BaseModel):
    diagnostics: list[Diagnostic]
    only: CodeActionKind | None
    trigger_kind: CodeActionTriggerKind


class Diagnostic(BaseModel): ...
