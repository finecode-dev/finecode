from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_lint.apply_code_actions_action import CodeActionOperation

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
    id="ResolveCodeActionRunContext"
)


@dataclasses.dataclass
class ResolveCodeActionRunPayload(code_action.RunActionPayload):
    provider: str
    """Which provider minted ``action_id`` (``CodeAction.provider``). Routes the
    request to exactly one handler; every other handler returns
    ``operations=None`` untouched."""

    action_id: str
    """Provider-local identifier to resolve, as returned on the original
    ``CodeAction``."""

    file_path: ResourceUri
    """File the action belongs to."""

    file_version: str | None = None
    """Version ``action_id`` was computed against. None means resolve against
    current content and report the version used."""


@dataclasses.dataclass
class ResolveCodeActionRunResult(code_action.RunActionResult):
    file_version: str = ""
    """Content version resolution was performed against."""

    operations: list[CodeActionOperation] | None = None
    """Ordered effect of the resolved action (design note D11) -- what apply
    consumes. None means no provider claimed this action_id, or it no longer
    exists."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ResolveCodeActionRunResult):
            return
        if self.operations is None:
            self.operations = other.operations
            if other.operations is not None:
                self.file_version = other.file_version
        # else: exactly one provider owns an action_id (design note D1); a later
        # non-None contribution would indicate a routing bug. Keep the first
        # winner rather than letting merge order decide.

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class ResolveCodeActionRunContext(
    code_action.RunActionContext[ResolveCodeActionRunPayload]
):
    """Pins the file's base version for the whole run.

    Same rationale as ``GetLintFixesRunContext`` / ``GetCodeActionsRunContext``
    (design note D6): read once in ``init()`` so every provider's resolve handler
    agrees on the content it is resolving against, without excluding other
    readers via a claim.
    """

    def __init__(
        self,
        run_id: int,
        initial_payload: ResolveCodeActionRunPayload,
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


class ResolveCodeActionAction(
    code_action.Action[
        ResolveCodeActionRunPayload,
        ResolveCodeActionRunContext,
        ResolveCodeActionRunResult,
    ]
):
    """Recover a code action's edits from its provider and action id.

    Serves the LSP ``codeAction/resolve`` endpoint, and is the lazy-resolution
    path a provider may use instead of embedding edits inline (design note D7).
    """

    DESCRIPTION = "Recover a code action's edits from its provider and action id."
    PAYLOAD_TYPE = ResolveCodeActionRunPayload
    RUN_CONTEXT_TYPE = ResolveCodeActionRunContext
    RESULT_TYPE = ResolveCodeActionRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
