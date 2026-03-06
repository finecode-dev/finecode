from __future__ import annotations

import sys
from pathlib import Path
from typing import override

from finecode_extension_api import service
from finecode_extension_api.actions import lint_files as lint_files_action
from finecode_extension_api.interfaces import ifileeditor, ilspclient, ilogger
from finecode_extension_api.contrib.lsp_service import LspService, map_diagnostics_to_lint_messages


class PyreflyLspService(service.DisposableService):
    """Pyrefly LSP service — thin wrapper around generic LspService."""

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        pyrefly_bin = Path(sys.executable).parent / "pyrefly"
        self._lsp_service = LspService(
            lsp_client=lsp_client,
            file_editor=file_editor,
            logger=logger,
            cmd=f"{pyrefly_bin} lsp",
            language_id="python",
            readable_id="pyrefly-lsp",
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
            raw_diagnostics, default_source="pyrefly"
        )
