from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_lint.lint_fix import (
    LintFix,
    Range,
)

_FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="GetLintFixesRunContext")


@dataclasses.dataclass
class GetLintFixesRunPayload(code_action.RunActionPayload):
    file_path: ResourceUri
    """File to compute fixes for."""

    range: Range | None = None
    """Restrict fixes to diagnostics overlapping this range. None means whole file."""

    diagnostic_codes: list[str] | None = None
    """Restrict fixes to diagnostics with these codes. None means all codes."""

    kinds: list[str] | None = None
    """LSP 'only' filter. Values: 'quickfix', 'source.fixAll', 'source.organizeImports', ...
    None means all kinds. Handlers use this to skip expensive work — e.g. a fix-on-save
    request with kinds=['source.fixAll'] should not compute interactive quickfixes."""

    file_version: str | None = None
    """IFileEditor content version. Handlers may reject stale requests by returning
    an empty result rather than recomputing against the wrong content. None = current."""


@dataclasses.dataclass
class GetLintFixesRunResult(code_action.RunActionResult):
    file_version: str = ""
    """Content version the returned fixes apply to. Callers compare this against the
    version they passed (or the current version) to detect races."""

    fixes: list[LintFix] = dataclasses.field(default_factory=list)

    version_diverged: bool = False
    """True once a merged contribution's ``file_version`` did not match ``self``'s.
    The run context pins one ``file_version`` for the whole run (see
    ``GetLintFixesRunContext``), so divergence means a handler observed the file at a
    different version than the one that was pinned — e.g. because it read the file
    itself instead of trusting the run context. A result with ``version_diverged=True``
    mixes fixes computed against two different contents and MUST NOT be used as an
    apply batch: its edits are not simultaneously interpretable against one base
    version."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetLintFixesRunResult):
            return
        if (
            other.file_version
            and self.file_version
            and other.file_version != self.file_version
        ):
            self.version_diverged = True
        self.fixes.extend(other.fixes)
        # Keep self.file_version -- the run's pinned version, not the merged
        # contribution's (see GetLintFixesRunContext.file_version).

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetLintFixesRunContext(code_action.RunActionContext[GetLintFixesRunPayload]):
    """Pins the file's base version for the whole run.

    Read once in ``init()`` via ``IFileEditor.read_file_version`` rather than left
    to each handler, so that concurrent handlers computing fixes for the same file
    agree on the content they are fixing (ADR-0083 rule 3). This is a read path: the
    file is never claimed with ``modify_file``, since a claim would exclude every
    other reader of the file for the duration of the run.
    """

    def __init__(
        self,
        run_id: int,
        initial_payload: GetLintFixesRunPayload,
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


class GetLintFixesAction(
    code_action.Action[
        GetLintFixesRunPayload, GetLintFixesRunContext, GetLintFixesRunResult
    ]
):
    """Compute fixes for linter diagnostics in a file."""

    DESCRIPTION = "Compute fixes for linter diagnostics in a file."
    PAYLOAD_TYPE = GetLintFixesRunPayload
    RUN_CONTEXT_TYPE = GetLintFixesRunContext
    RESULT_TYPE = GetLintFixesRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
