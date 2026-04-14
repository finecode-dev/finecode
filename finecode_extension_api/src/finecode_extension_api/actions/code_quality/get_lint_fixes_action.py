from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality.lint_fix import (
    LintFix,
    Range,
)
from finecode_extension_api.resource_uri import ResourceUri


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

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetLintFixesRunResult):
            return
        self.fixes.extend(other.fixes)
        # Keep self.file_version (same file, same run; versions must match)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetLintFixesRunContext(
    code_action.RunActionContext[GetLintFixesRunPayload]
): ...


class GetLintFixesAction(
    code_action.Action[
        GetLintFixesRunPayload, GetLintFixesRunContext, GetLintFixesRunResult
    ]
):
    """Compute fixes for linter diagnostics in a file."""

    PAYLOAD_TYPE = GetLintFixesRunPayload
    RUN_CONTEXT_TYPE = GetLintFixesRunContext
    RESULT_TYPE = GetLintFixesRunResult
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT
