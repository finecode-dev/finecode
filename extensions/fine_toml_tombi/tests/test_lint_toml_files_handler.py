"""Tests for how ``TombiLintTomlFilesHandler`` reports an unavailable language server.

The LSP service is a stub: these tests exercise the handler's failure channel,
not tombi itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fine_lint.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api import code_action
from finecode_extension_api.contrib.lsp_service import (
    LspDiagnosticsUnavailable,
    LspDiagnosticsUnavailableCause,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_toml_tombi.lint_toml_files_handler import (
    TombiLintTomlFilesHandler,
    TombiLintTomlFilesHandlerConfig,
)


class _StubProjectInfoProvider:
    def get_current_project_dir_path(self) -> Path:
        return Path("/fake/project")


class _StubLspService:
    def __init__(self) -> None:
        self.check_error: Exception | None = None
        self.check_error_once: Exception | None = None
        self.checked: list[Path] = []
        self.ensure_started_calls: list[str] = []

    async def ensure_started(self, root_uri: str) -> None:
        self.ensure_started_calls.append(root_uri)

    async def check_file(self, file_path: Path, timeout: float = 30.0) -> list[Any]:
        self.checked.append(file_path)
        if self.check_error is not None:
            raise self.check_error
        if self.check_error_once is not None:
            error, self.check_error_once = self.check_error_once, None
            raise error
        return []


async def test_a_dead_language_server_yields_a_file_error_not_an_ok_result(
    tmp_path: Path,
) -> None:
    """A failed server must surface on the file, never as an empty (clean) result.

    An empty diagnostics list renders as ``: OK`` and leaves the run's return
    code at SUCCESS, so a dead server would look exactly like clean code. The
    error diagnostic is what makes the failure visible and the return code
    non-zero.
    """
    subject = (tmp_path / "pyproject.toml").resolve()
    uri = path_to_resource_uri(subject)
    lsp = _StubLspService()
    lsp.check_error = LspDiagnosticsUnavailable(
        LspDiagnosticsUnavailableCause.SERVER_EXITED
    )
    handler = TombiLintTomlFilesHandler(
        config=TombiLintTomlFilesHandlerConfig(),
        project_info_provider=_StubProjectInfoProvider(),  # type: ignore[arg-type]
        lsp_service=lsp,  # type: ignore[arg-type]
    )

    result = await handler.run_on_single_file(uri)

    assert isinstance(result, DiagnosticFilesRunResult)
    diagnostics = result.messages[uri]
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "server-unavailable"
    assert diagnostics[0].source == "tombi"
    assert result.return_code is code_action.RunReturnCode.ERROR
    assert lsp.checked == [subject, subject]
    assert len(lsp.ensure_started_calls) == 2


async def test_one_transient_server_failure_is_retried_once(tmp_path: Path) -> None:
    """A single death must be retried once, and only once.

    The server is restarted on the next demand, so a failure caused by one dead
    session is recoverable; retrying more than once would turn a broken server
    into an unbounded loop instead of a visible error.
    """
    subject = (tmp_path / "pyproject.toml").resolve()
    uri = path_to_resource_uri(subject)
    lsp = _StubLspService()
    lsp.check_error_once = LspDiagnosticsUnavailable(
        LspDiagnosticsUnavailableCause.SERVER_EXITED
    )
    handler = TombiLintTomlFilesHandler(
        config=TombiLintTomlFilesHandlerConfig(),
        project_info_provider=_StubProjectInfoProvider(),  # type: ignore[arg-type]
        lsp_service=lsp,  # type: ignore[arg-type]
    )

    result = await handler.run_on_single_file(uri)

    assert isinstance(result, DiagnosticFilesRunResult)
    assert result.messages[uri] == []
    assert result.return_code is code_action.RunReturnCode.SUCCESS
    assert lsp.checked == [subject, subject]
    assert len(lsp.ensure_started_calls) == 2
