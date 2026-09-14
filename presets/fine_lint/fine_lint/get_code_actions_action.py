from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_lint.code_action_types import (
    CodeAction,
    CodeActionTriggerKind,
    DiagnosticRef,
)
from fine_lint.lint_fix import Range

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="GetCodeActionsRunContext")


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

    version_diverged: bool = False
    """True once a merged contribution's ``file_version`` did not match ``self``'s.
    The run context pins one ``file_version`` for the whole run (see
    ``GetCodeActionsRunContext``); divergence means a handler observed the file at a
    different version than the one that was pinned. A result with
    ``version_diverged=True`` mixes actions computed against two different contents
    and MUST NOT be used as an apply batch: its edits are not simultaneously
    interpretable against one base version."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetCodeActionsRunResult):
            return
        if (
            other.file_version
            and self.file_version
            and other.file_version != self.file_version
        ):
            self.version_diverged = True
        self.actions.extend(other.actions)
        # Keep self.file_version -- the run's pinned version, not the merged
        # contribution's (see GetCodeActionsRunContext.file_version).

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetCodeActionsRunContext(code_action.RunActionContext[GetCodeActionsRunPayload]):
    """Pins the file's base version for the whole run.

    Read once in ``init()`` via ``IFileEditor.read_file_version`` rather than left to
    each handler, so that concurrent handlers contributing code actions for the same
    file agree on the content they were computed against (ADR-0083 rule 3). This is a
    read path: the file is never claimed with ``modify_file``, since a claim would
    exclude every other reader of the file for the duration of the run.
    """

    def __init__(
        self,
        run_id: int,
        initial_payload: GetCodeActionsRunPayload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        file_editor: ifileeditor.IFileEditor,
        partial_result_sender: code_action.PartialResultSender = code_action._NOOP_SENDER,
        progress_sender: code_action.ProgressSender = code_action._NOOP_PROGRESS_SENDER,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
            partial_result_sender=partial_result_sender,
            progress_sender=progress_sender,
        )
        self._file_editor = file_editor
        self.file_version: str

    async def init(self) -> None:
        file_path = resource_uri_to_path(self.initial_payload.file_path)
        async with self._file_editor.session(_FILE_OPERATION_AUTHOR) as session:
            self.file_version = await session.read_file_version(file_path)


class GetCodeActionsAction(
    code_action.Action[
        GetCodeActionsRunPayload, GetCodeActionsRunContext, GetCodeActionsRunResult
    ]
):
    """Return code actions (quickfixes, refactorings, source actions) for a location."""

    DESCRIPTION = (
        "Return code actions (quickfixes, refactorings, source actions) for a location."
    )
    PAYLOAD_TYPE = GetCodeActionsRunPayload
    RUN_CONTEXT_TYPE = GetCodeActionsRunContext
    RESULT_TYPE = GetCodeActionsRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
