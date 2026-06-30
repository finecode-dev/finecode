import enum
import dataclasses

from finecode_extension_api import code_action


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


@dataclasses.dataclass
class Diagnostic: ...


@dataclasses.dataclass
class CodeActionContext:
    diagnostics: list[Diagnostic]
    only: CodeActionKind | None
    trigger_kind: CodeActionTriggerKind
