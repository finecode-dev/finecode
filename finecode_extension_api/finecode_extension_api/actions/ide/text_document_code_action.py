import enum

from finecode_extension_api import code_action, common_types


class CodeActionPayload(code_action.RunActionPayload):
    text_document: common_types.TextDocumentIdentifier
    range: common_types.Range


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


class Diagnostic(code_action.BaseModel): ...


class CodeActionContext(code_action.BaseModel):
    diagnostics: list[Diagnostic]
    only: CodeActionKind | None
    trigger_kind: CodeActionTriggerKind
