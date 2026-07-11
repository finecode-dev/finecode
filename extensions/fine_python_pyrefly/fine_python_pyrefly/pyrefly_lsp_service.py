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
from finecode_extension_api.interfaces import (
    ifileeditor,
    ilspclient,
    ilogger,
    iextensionrunnerinfoprovider,
)
from finecode_extension_api.contrib.lsp_service import LspService
from fine_inspect_code.diagnostic_types import map_lsp_diagnostics


_PYREFLY_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "didSave": True,
        },
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
        "inlayHint": {"dynamicRegistration": False},
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
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
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
        # pyrefly's own environment/interpreter auto-detection does not know about
        # FineCode's per-project envs, so without this it resolves imports against the
        # wrong (or no) site-packages. Applied here so every feature (hover, definition,
        # inlay hints, ...) gets it, not just type checking.
        #
        # A single LSP server can only be configured with one resolution env at startup
        # (it cannot switch per file), so the broadest env is used: "dev" is a superset
        # of "runtime" (project deps + dev-only deps such as pytest), which lets test
        # files resolve their imports too. Falling back to "runtime" when "dev" does not
        # exist only loses symbols, never adds false ones.
        #
        # Trade-off: dev-only deps become resolvable from source files too, so pyrefly no
        # longer flags a dev dependency imported from source. That boundary is enforced
        # separately by a dependency-hygiene tool (e.g. deptry). "dev"/"runtime" are
        # naming conventions (env labels are arbitrary); this could be made configurable
        # in the future, per handler or at the action level.
        self._pyrefly_settings: dict[str, Any] = {}
        resolution_env = "dev"
        venv_dir = extension_runner_info_provider.get_venv_dir_path_of_env(
            resolution_env
        )
        if not venv_dir.exists():
            resolution_env = "runtime"
            venv_dir = extension_runner_info_provider.get_venv_dir_path_of_env(
                resolution_env
            )
        logger.debug(f"pyrefly resolves imports against the '{resolution_env}' env")
        interpreter_path = extension_runner_info_provider.get_venv_python_interpreter(
            venv_dir
        )
        site_packages = extension_runner_info_provider.get_venv_site_packages(venv_dir)
        self.update_settings(
            {
                "pythonPath": str(interpreter_path),
                "pyrefly": {"extraPaths": [str(p) for p in site_packages]},
            }
        )

    @override
    async def init(self) -> None:
        await self._lsp_service.init()

    @override
    def dispose(self) -> None:
        self._lsp_service.dispose()

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Update pyrefly LSP settings.

        The underlying LspService.update_settings merges the top-level dict
        shallowly, which would let a later call overwrite the whole "pyrefly"
        section (e.g. dropping extraPaths when a handler adds displayTypeErrors).
        Deep-merge that one section here instead.
        """
        pyrefly_settings = settings.get("pyrefly")
        if pyrefly_settings is not None:
            self._pyrefly_settings = {**self._pyrefly_settings, **pyrefly_settings}
            settings = {**settings, "pyrefly": self._pyrefly_settings}
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

    async def get_inlay_hints(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_inlay_hints(
            file_path, content, range_dict, timeout=timeout
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
