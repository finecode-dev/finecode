from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, override

from finecode_extension_api import service
from fine_lint.diagnostic_types import Diagnostic
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SEMANTIC_TOKEN_MODIFIERS,
    SEMANTIC_TOKEN_TYPES,
)
from finecode_extension_api.interfaces import ifileeditor, ilspclient, ilogger
from finecode_extension_api.contrib.lsp_service import LspService, apply_text_edits
from fine_inspect_code.diagnostic_types import map_lsp_diagnostics


_TOMBI_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "didSave": True,
        },
        "publishDiagnostics": {"relatedInformation": True},
        "semanticTokens": {
            "dynamicRegistration": False,
            "tokenTypes": SEMANTIC_TOKEN_TYPES,
            "tokenModifiers": SEMANTIC_TOKEN_MODIFIERS,
            "formats": ["relative"],
            # tombi doesn't support range requests, keep only full
            "requests": {"full": True},
            "multilineTokenSupport": False,
            "overlappingTokenSupport": False,
        },
    },
    "workspace": {
        # workspaceFolders must stay False: LspService has no workspace/workspaceFolders
        # request handler. Declaring True would tell tombi we support the pull-based
        # request, but we pass folders once in initialize — no dynamic updates needed.
        "workspaceFolders": False,
        "configuration": True,
    },
}


class TombiLspService(service.DisposableService):
    """Tombi LSP service — thin wrapper around generic LspService."""

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        tombi_bin = Path(sys.executable).parent / "tombi"
        self._lsp_service = LspService(
            lsp_client=lsp_client,
            file_editor=file_editor,
            logger=logger,
            cmd=f"{tombi_bin} lsp",
            language_id="toml",
            readable_id="tombi-lsp",
            client_capabilities=_TOMBI_CLIENT_CAPABILITIES,
        )

    @override
    async def init(self) -> None:
        await self._lsp_service.init()

    @override
    def dispose(self) -> None:
        self._lsp_service.dispose()

    async def ensure_started(self, root_uri: str) -> None:
        await self._lsp_service.ensure_started(root_uri)

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> list[Diagnostic]:
        raw_diagnostics = await self._lsp_service.check_file(file_path, timeout)
        return map_lsp_diagnostics(raw_diagnostics, default_source="tombi")

    async def format_file(
        self,
        file_path: Path,
        file_content: str,
        timeout: float = 30.0,
    ) -> str:
        raw_edits = await self._lsp_service.format_file(file_path, file_content, timeout=timeout)
        if not raw_edits:
            return file_content
        return apply_text_edits(file_content, raw_edits)

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._lsp_service.server_capabilities

    async def get_semantic_tokens(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        return await self._lsp_service.get_semantic_tokens(
            file_path, content, range_dict=range_dict, timeout=timeout
        )
