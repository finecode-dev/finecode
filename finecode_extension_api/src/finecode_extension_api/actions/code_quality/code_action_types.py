from __future__ import annotations

import dataclasses
import enum

from finecode_extension_api.actions.code_quality.lint_fix import Range, TextEdit
from finecode_extension_api.resource_uri import ResourceUri


class CodeActionTriggerKind(enum.IntEnum):
    INVOKED = 1
    AUTOMATIC = 2


@dataclasses.dataclass
class DiagnosticRef:
    range: Range
    codes: list[str]
    """LintMessage codes at this range (list because a single diagnostic may carry multiple
    related codes; usually length 1)."""


@dataclasses.dataclass
class CodeAction:
    action_id: str
    """Identifier for lazy resolve. See LintFix.fix_id semantics."""

    title: str
    kind: str

    edits: dict[ResourceUri, list[TextEdit]] | None = None
    """None means the bridge/handler returned a stub and the IDE must call resolve."""

    diagnostics: list[DiagnosticRef] = dataclasses.field(default_factory=list)
    """The diagnostics this action addresses; empty for refactorings and source actions
    not tied to a diagnostic."""

    is_preferred: bool = False
