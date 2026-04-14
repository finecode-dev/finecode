from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.code_action_types import (
    CodeAction,
    CodeActionTriggerKind,
    DiagnosticRef,
)
from finecode_extension_api.actions.code_quality.lint_fix import Range
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class GetCodeActionsRunPayload(code_action.RunActionPayload):
    file_path: ResourceUri
    range: Range
    """Selection or cursor position from the IDE."""

    diagnostics: list[DiagnosticRef]
    """Diagnostics the IDE was showing at 'range'. Each ref carries enough to identify
    the corresponding lint message (range + code). Opaque data fields are not used."""

    only: list[str] | None = None
    """LSP 'only' filter — 'quickfix', 'refactor', 'refactor.extract', 'source.fixAll', etc.
    None means all kinds."""

    trigger_kind: CodeActionTriggerKind = CodeActionTriggerKind.INVOKED
    file_version: str | None = None


@dataclasses.dataclass
class GetCodeActionsRunResult(code_action.RunActionResult):
    file_version: str = ""
    actions: list[CodeAction] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetCodeActionsRunResult):
            return
        self.actions.extend(other.actions)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetCodeActionsRunContext(
    code_action.RunActionContext[GetCodeActionsRunPayload]
): ...


class GetCodeActionsAction(
    code_action.Action[
        GetCodeActionsRunPayload, GetCodeActionsRunContext, GetCodeActionsRunResult
    ]
):
    """Return code actions (quickfixes, refactorings, source actions) for a location."""

    PAYLOAD_TYPE = GetCodeActionsRunPayload
    RUN_CONTEXT_TYPE = GetCodeActionsRunContext
    RESULT_TYPE = GetCodeActionsRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
