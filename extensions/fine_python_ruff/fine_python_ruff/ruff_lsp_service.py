from __future__ import annotations

import asyncio
import collections.abc
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from fine_inspect_code.diagnostic_types import map_lsp_diagnostics
from fine_lint.diagnostic_types import Diagnostic
from finecode_extension_api import code_action, service
from finecode_extension_api.contrib.lsp_service import LspService, apply_text_edits
from finecode_extension_api.interfaces import ifileeditor, ilogger, ilspclient

SettingsProvider = collections.abc.Callable[
    [code_action.RunActionMeta],
    collections.abc.Awaitable[dict[str, Any]],
]
"""Builds one handler's contribution to the shared server's settings.

Async because a handler's settings can depend on running another action (the language
level comes from ``get_src_artifact_toolchain_range``), and given the run's meta because
that is what a nested action call needs. It may be invoked from the run of a *different*
handler -- whichever one reaches the server first -- so it must not depend on its own
handler having run."""

_RUFF_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "synchronization": {
            "dynamicRegistration": False,
            "didSave": True,
        },
        "completion": {"dynamicRegistration": False},
        "hover": {"dynamicRegistration": False},
        "publishDiagnostics": {"relatedInformation": True},
        # Pull diagnostics. Ruff advertises `diagnosticProvider` in response and
        # `LspService` then asks for a document's diagnostics instead of waiting
        # to be told about them -- the answer belongs to the request, so none of
        # the push path's guesses apply. It also stops pushing once this is
        # declared, which is the spec's intent and costs nothing here: nothing
        # in this runner consumes unsolicited diagnostics.
        #
        # `relatedDocumentSupport` stays False: ruff reports
        # `interFileDependencies: false`, so a file's diagnostics never depend
        # on another file, and accepting related documents would only add
        # results nobody asked about.
        "diagnostic": {
            "dynamicRegistration": False,
            "relatedDocumentSupport": False,
        },
        # No dataSupport and no resolveSupport, deliberately. Declaring both tells
        # ruff the client will fetch edits through codeAction/resolve, and it then
        # answers with actions that carry no edit at all. An empty edit set is a
        # legal LintFix (display-only fixes exist), so that arrives as fixes which
        # look applicable and change nothing -- silently. Inline edits instead.
        "codeAction": {
            "dynamicRegistration": False,
            "codeActionLiteralSupport": {
                "codeActionKind": {
                    "valueSet": [
                        "quickfix",
                        "source.fixAll",
                        "source.organizeImports",
                    ],
                },
            },
        },
    },
    "workspace": {
        "workspaceFolders": True,
        "configuration": True,
    },
}


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge *source* into *target*, recursing into nested dicts.

    Contributions overlap in nesting rather than in leaves: the linter fills
    ``configuration.target-version`` and the formatter ``configuration.format``. A flat
    update would let whichever ran last replace the other's whole sub-table.
    """
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value)
        else:
            target[key] = value


class RuffLspService(service.DisposableService):
    """Ruff LSP service — thin wrapper around generic LspService.

    One instance is shared by every ruff handler in a runner (the lint handler, the
    formatter and the code-action handler all resolve to the same DI singleton), and
    each of them has settings to contribute. Ruff reads client settings **only** from
    the ``initialize`` handshake: its ``workspace/didChangeConfiguration`` handler does
    nothing, so a setting registered after the server started is silently lost for the
    runner's lifetime. Whichever handler runs first would therefore decide what the
    other two get, and format-on-save alone is enough to make that ordering vary.

    So contributions are *providers* rather than pushes: a handler registers one in
    ``__init__`` (nothing async happens there) and ``ensure_started`` runs all of them
    before the server is launched. Every handler's settings are in place no matter which
    one reaches the server first.
    """

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
    ) -> None:
        ruff_bin = Path(sys.executable).parent / "ruff"
        self._logger = logger
        self._lsp_service = LspService(
            lsp_client=lsp_client,
            file_editor=file_editor,
            logger=logger,
            cmd=f"{ruff_bin} server",
            language_id="python",
            readable_id="ruff-lsp",
            client_capabilities=_RUFF_CLIENT_CAPABILITIES,
            # `empty_diagnostics_settle_sec` is left at its default and never
            # reached: the capability declared above puts this service on the
            # pull path, where an empty answer means a clean file and nothing
            # has to be waited out to find that out.
        )
        self._settings_providers: list[SettingsProvider] = []
        self._settings_resolved = False
        self._settings_lock = asyncio.Lock()

    @override
    async def init(self) -> None:
        await self._lsp_service.init()

    @override
    def dispose(self) -> None:
        self._lsp_service.dispose()

    def add_settings_provider(self, provider: SettingsProvider) -> None:
        """Register a contribution to the settings the server is started with."""
        if self._settings_resolved:
            # the server is configured for good by then, so the contribution can only be
            # dropped. Say so: the symptom otherwise is a handler's whole configuration
            # -- its rule selection, its line length -- quietly not applying.
            self._logger.warning(
                "A ruff settings provider was registered after the server was already"
                " configured; its settings will not apply. Ruff only reads client"
                " settings during initialize, so all handlers must be constructed"
                " before the first one runs."
            )
            return
        self._settings_providers.append(provider)

    async def ensure_started(
        self, root_uri: str, meta: code_action.RunActionMeta
    ) -> None:
        await self._resolve_settings(meta)
        await self._lsp_service.ensure_started(root_uri)

    async def _resolve_settings(self, meta: code_action.RunActionMeta) -> None:
        if self._settings_resolved:
            return

        async with self._settings_lock:
            if self._settings_resolved:
                return

            settings: dict[str, Any] = {}
            for provider in self._settings_providers:
                _deep_merge(settings, await provider(meta))

            self._lsp_service.update_settings(settings)
            self._settings_resolved = True

    async def get_code_actions(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any],
        *,
        only: list[str] | None = None,
        diagnostic_codes: list[str] | None = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        return await self._lsp_service.get_code_actions(
            file_path,
            content,
            range_dict,
            only=only,
            diagnostic_codes=diagnostic_codes,
            timeout=timeout,
        )

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> list[Diagnostic]:
        raw_diagnostics = await self._lsp_service.check_file(file_path, timeout)
        return map_lsp_diagnostics(raw_diagnostics, default_source="ruff")

    async def format_file(
        self,
        file_path: Path,
        file_content: str,
        timeout: float = 30.0,
    ) -> str:
        """Format a file via LSP and return the formatted content."""
        raw_edits = await self._lsp_service.format_file(
            file_path, file_content, timeout=timeout
        )
        if not raw_edits:
            return file_content
        return apply_text_edits(file_content, raw_edits)
