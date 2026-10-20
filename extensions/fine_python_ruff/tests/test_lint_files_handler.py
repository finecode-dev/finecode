"""Tests for how ``RuffLintFilesHandler`` reports an unavailable language server.

The LSP service is a stub: these tests exercise the handler's failure channel,
not ruff itself.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fine_lint.diagnostic_types import DiagnosticFilesRunResult
from finecode_extension_api import code_action
from finecode_extension_api.contrib.lsp_service import (
    LspDiagnosticsUnavailable,
    LspDiagnosticsUnavailableCause,
)
from finecode_extension_api.interfaces import icache, ifileeditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger

from fine_python_ruff.lint_files_handler import (
    RuffLintFilesHandler,
    RuffLintFilesHandlerConfig,
)


class _FakeCache:
    def __init__(self) -> None:
        self.saved: list[tuple[Path, str, str]] = []

    async def get_file_cache(self, file_path: Path, key: str) -> Any:
        raise icache.CacheMissException()

    async def save_file_cache(
        self, file_path: Path, version: str, key: str, messages: Any
    ) -> None:
        self.saved.append((file_path, version, key))


class _FakeFileEditorSession:
    @contextlib.asynccontextmanager
    async def read_file(self, file_path: Path) -> AsyncIterator[ifileeditor.FileInfo]:
        yield ifileeditor.FileInfo(content="x = 1\n", version="1")


class _FakeFileEditor:
    @contextlib.asynccontextmanager
    async def session(self, author: Any) -> AsyncIterator[_FakeFileEditorSession]:
        yield _FakeFileEditorSession()


class _StubProjectInfoProvider:
    def get_current_project_dir_path(self) -> Path:
        return Path("/fake/project")


class _StubLspService:
    def __init__(self) -> None:
        self.check_error: Exception | None = None
        self.check_error_once: Exception | None = None
        self.checked: list[Path] = []
        self.ensure_started_calls: list[tuple[str, Any]] = []

    def add_settings_provider(self, provider: Any) -> None:
        pass

    async def ensure_started(self, root_uri: str, meta: Any) -> None:
        self.ensure_started_calls.append((root_uri, meta))

    async def check_file(self, file_path: Path, timeout: float = 30.0) -> list[Any]:
        self.checked.append(file_path)
        if self.check_error is not None:
            raise self.check_error
        if self.check_error_once is not None:
            error, self.check_error_once = self.check_error_once, None
            raise error
        return []


def _make_handler(lsp: _StubLspService) -> RuffLintFilesHandler:
    return RuffLintFilesHandler(
        config=RuffLintFilesHandlerConfig(),
        cache=_FakeCache(),  # type: ignore[arg-type]
        logger=NoOpLogger(),
        file_editor=_FakeFileEditor(),  # type: ignore[arg-type]
        command_runner=None,  # type: ignore[arg-type]
        project_info_provider=_StubProjectInfoProvider(),  # type: ignore[arg-type]
        action_runner=None,  # type: ignore[arg-type]
        lsp_service=lsp,  # type: ignore[arg-type]
    )


async def test_a_dead_language_server_yields_a_file_error_not_an_ok_result(
    tmp_path: Path,
) -> None:
    """A failed server must surface on the file, never as an empty (clean) result.

    An empty diagnostics list renders as ``: OK`` and leaves the run's return
    code at SUCCESS, so a dead server would look exactly like clean code. The
    error diagnostic is what makes the failure visible and the return code
    non-zero, and the failure must not be written to the per-file cache either —
    the next run has to ask the server again.
    """
    subject = (tmp_path / "subject.py").resolve()
    uri = path_to_resource_uri(subject)
    lsp = _StubLspService()
    lsp.check_error = LspDiagnosticsUnavailable(
        LspDiagnosticsUnavailableCause.SERVER_EXITED
    )
    handler = _make_handler(lsp)
    meta = code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )
    result = await handler.run_on_single_file(uri, meta)

    assert isinstance(result, DiagnosticFilesRunResult)
    diagnostics = result.messages[uri]
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "server-unavailable"
    assert diagnostics[0].source == "ruff"
    assert result.return_code is code_action.RunReturnCode.ERROR
    assert handler.cache.saved == []  # type: ignore[attr-defined]
    assert lsp.checked == [subject, subject]
    assert len(lsp.ensure_started_calls) == 2
    assert lsp.ensure_started_calls[0][1] is meta
    assert lsp.ensure_started_calls[1][1] is meta


async def test_one_transient_server_failure_is_retried_once(tmp_path: Path) -> None:
    """A single death must be retried once, and only once.

    The server is restarted on the next demand, so a failure caused by one dead
    session is recoverable; retrying more than once would turn a broken server
    into an unbounded loop instead of a visible error.
    """
    subject = (tmp_path / "subject.py").resolve()
    uri = path_to_resource_uri(subject)
    lsp = _StubLspService()
    lsp.check_error_once = LspDiagnosticsUnavailable(
        LspDiagnosticsUnavailableCause.SERVER_EXITED
    )
    handler = _make_handler(lsp)
    meta = code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )
    result = await handler.run_on_single_file(uri, meta)

    assert isinstance(result, DiagnosticFilesRunResult)
    assert result.messages[uri] == []
    assert result.return_code is code_action.RunReturnCode.SUCCESS
    assert lsp.checked == [subject, subject]
    assert len(lsp.ensure_started_calls) == 2
    assert lsp.ensure_started_calls[0][1] is meta
    assert lsp.ensure_started_calls[1][1] is meta
