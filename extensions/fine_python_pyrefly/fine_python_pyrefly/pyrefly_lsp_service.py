from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, override

from finecode_extension_api import service
from fine_lint.diagnostic_types import Diagnostic
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SEMANTIC_TOKEN_TYPES,
    SEMANTIC_TOKEN_MODIFIERS,
)
from finecode_extension_api.interfaces import ifileeditor, ilspclient, ilogger
from finecode_extension_api.contrib.lsp_service import LspService
from fine_inspect_code.diagnostic_types import map_lsp_diagnostics


_PYREFLY_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "didSave": True,
        },
        "completion": {"dynamicRegistration": False},
        "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
        "publishDiagnostics": {"relatedInformation": True},
        "semanticTokens": {
            "dynamicRegistration": False,
            "tokenTypes": SEMANTIC_TOKEN_TYPES,
            "tokenModifiers": SEMANTIC_TOKEN_MODIFIERS,
            "formats": ["relative"],
            "requests": {"full": True, "range": True},
            "multilineTokenSupport": False,
            "overlappingTokenSupport": False,
        },
        "definition": {"dynamicRegistration": False, "linkSupport": False},
        "references": {"dynamicRegistration": False},
        "typeDefinition": {"dynamicRegistration": False, "linkSupport": False},
        "implementation": {"dynamicRegistration": False, "linkSupport": False},
        "documentHighlight": {"dynamicRegistration": False},
        "callHierarchy": {
            "dynamicRegistration": False,
        },
        "typeHierarchy": {
            "dynamicRegistration": False,
        },
    },
    "workspace": {
        "workspaceFolders": True,
        "configuration": True,
    },
}


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
            client_capabilities=_PYREFLY_CLIENT_CAPABILITIES,
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
    ) -> list[Diagnostic]:
        raw_diagnostics = await self._lsp_service.check_file(file_path, timeout)
        return map_lsp_diagnostics(
            raw_diagnostics, default_source="pyrefly"
        )

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._lsp_service.server_capabilities

    async def get_hover(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        return await self._lsp_service.get_hover(
            file_path, content, position, timeout=timeout
        )

    async def get_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        return await self._lsp_service.get_definition(
            file_path, content, position, timeout=timeout
        )

    async def get_references(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        include_declaration: bool = True,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_references(
            file_path, content, position, include_declaration=include_declaration, timeout=timeout
        )

    async def get_type_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        return await self._lsp_service.get_type_definition(
            file_path, content, position, timeout=timeout
        )

    async def get_implementation(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        return await self._lsp_service.get_implementation(
            file_path, content, position, timeout=timeout
        )

    async def get_document_highlight(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_document_highlight(
            file_path, content, position, timeout=timeout
        )

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

    async def get_call_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_call_hierarchy_prepare(
            file_path, content, position, timeout=timeout
        )

    async def get_call_hierarchy_incoming_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_call_hierarchy_incoming_calls(
            file_path, content, item, timeout=timeout
        )

    async def get_call_hierarchy_outgoing_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_call_hierarchy_outgoing_calls(
            file_path, content, item, timeout=timeout
        )

    async def get_type_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_type_hierarchy_prepare(
            file_path, content, position, timeout=timeout
        )

    async def get_type_hierarchy_supertypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_type_hierarchy_supertypes(
            file_path, content, item, timeout=timeout
        )

    async def get_type_hierarchy_subtypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_type_hierarchy_subtypes(
            file_path, content, item, timeout=timeout
        )
