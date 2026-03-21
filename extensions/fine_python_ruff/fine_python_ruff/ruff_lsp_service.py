from __future__ import annotations

import sys
from pathlib import Path
from typing import override

from finecode_extension_api import service
from finecode_extension_api.actions.code_quality import lint_files_action
from finecode_extension_api.interfaces import ifileeditor, ilspclient, ilogger
from finecode_extension_api.contrib.lsp_service import LspService, map_diagnostics_to_lint_messages, apply_text_edits


class RuffLspService(service.DisposableService):
    """Ruff LSP service — thin wrapper around generic LspService."""

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        ruff_bin = Path(sys.executable).parent / "ruff"
        self._lsp_service = LspService(
            lsp_client=lsp_client,
            file_editor=file_editor,
            logger=logger,
            cmd=f"{ruff_bin} server",
            language_id="python",
            readable_id="ruff-lsp",
        )

    @override
    async def init(self) -> None:
        await self._lsp_service.init()

    @override
    def dispose(self) -> None:
        self._lsp_service.dispose()

    def update_settings(self, settings: dict[str, object]) -> None:
        self._lsp_service.update_settings(settings)

    async def ensure_started(self, root_uri: str) -> None:
        await self._lsp_service.ensure_started(root_uri)

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> list[lint_files_action.LintMessage]:
        raw_diagnostics = await self._lsp_service.check_file(file_path, timeout)
        return map_diagnostics_to_lint_messages(
            raw_diagnostics, default_source="ruff"
        )

    async def format_file(
        self,
        file_path: Path,
        file_content: str,
        timeout: float = 30.0,
    ) -> str:
        """Format a file via LSP and return the formatted content."""
        raw_edits = await self._lsp_service.format_file(file_path, file_content, timeout=timeout)
        if not raw_edits:
            return file_content
        return apply_text_edits(file_content, raw_edits)
