from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, override

from finecode_extension_api import service
from finecode_extension_api.actions.code_quality import lint_files_action
from finecode_extension_api.interfaces import ifileeditor, ilogger, ilspclient


class LspService(service.DisposableService):
    """Generic long-running LSP service with document synchronization.

    Document synchronization is optimized by IFileEditor events: open, change and close
    events are forwarded to the LSP server as textDocument/did* notifications.

    For files not opened by any session, check_file reads via file editor,
    compares the content version against what was last sent to LSP, and sends
    didOpen/didChange directly only when the content has changed.

    Settings management:
        Settings are managed via ``update_settings(settings)`` (sync) which merges
        into the internal ``_settings`` dict. Handlers call ``update_settings`` in
        their ``__init__`` to apply config-driven settings. Since handler
        instantiation happens during eager initialization (before the LSP server
        is started), settings accumulate. When ``ensure_started`` triggers
        ``start``, settings are delivered to the LSP server in three ways:

        1. ``initializationOptions`` in the ``initialize`` request (as
           ``{"settings": ...}``).
        2. ``workspace/didChangeConfiguration`` notification after ``initialized``.
        3. ``workspace/configuration`` pull requests from the server are answered
           with the current settings.

        To push settings to an already running server, call ``send_settings``.
    """

    def __init__(
        self,
        lsp_client: ilspclient.ILspClient,
        file_editor: ifileeditor.IFileEditor,
        logger: ilogger.ILogger,
        *,
        cmd: str,
        language_id: str,
        readable_id: str = "",
    ) -> None:
        self._lsp_client = lsp_client
        self._file_editor = file_editor
        self._logger = logger
        self._cmd = cmd
        self._language_id = language_id
        self._readable_id = readable_id
        self._file_operation_author = ifileeditor.FileOperationAuthor(
            id=readable_id or "LspService"
        )
        self._session: ilspclient.ILspSession | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._start_lock: asyncio.Lock = asyncio.Lock()
        # pending diagnostics waiters: uri -> Event (threading for cross-thread safety)
        self._diagnostics: dict[str, threading.Event] = {}
        # last received diagnostics per uri (persistent cache)
        self._diagnostics_data: dict[str, list[dict[str, Any]]] = {}
        # uri -> content version last sent to LSP (for change detection)
        self._file_versions: dict[str, str] = {}
        # uris currently open in the LSP server
        self._open_documents: set[str] = set()
        # LSP protocol version counter per uri
        self._document_version: dict[str, int] = {}
        # current settings, accumulated via update_settings and sent on start
        self._settings: dict[str, Any] = {}

    @override
    async def init(self) -> None:
        pass

    @override
    def dispose(self) -> None:
        asyncio.create_task(self._async_dispose())

    async def _async_dispose(self) -> None:
        if self._event_task is not None:
            self._event_task.cancel()
            try:
                await self._event_task
            except (asyncio.CancelledError, Exception):
                pass
            self._event_task = None

        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None

        self._diagnostics.clear()
        self._diagnostics_data.clear()
        self._file_versions.clear()
        self._open_documents.clear()
        self._document_version.clear()

    async def ensure_started(
        self,
        root_uri: str,
    ) -> None:
        async with self._start_lock:
            if self._session is not None:
                return
            await self.start(root_uri)

    async def start(
        self,
        root_uri: str,
    ) -> None:
        session = self._lsp_client.session(
            cmd=self._cmd,
            root_uri=root_uri,
            workspace_folders=[{"uri": root_uri, "name": root_uri}],
            initialization_options={"settings": self._settings}
            if self._settings
            else None,
            readable_id=self._readable_id,
        )
        await session.__aenter__()
        self._session = session
        self._session.on_notification(
            "textDocument/publishDiagnostics",
            self._handle_diagnostics,
        )
        # Handle pull-based configuration (e.g. pyrefly sends workspace/configuration
        # requests after initialized and after each didChangeConfiguration).
        self._session.on_request(
            "workspace/configuration",
            self._handle_configuration_request,
        )

        # some LSP servers read settings from didChangeConfiguration (e.g. pyrefly)
        if self._settings:
            await self._session.send_notification(
                "workspace/didChangeConfiguration",
                {"settings": self._settings},
            )

        ready = asyncio.Event()
        self._event_task = asyncio.create_task(self._run_event_loop(ready))
        await ready.wait()

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Update LSP server settings.

        Merges ``settings`` into the internal settings dict. If the server is not
        yet started, settings accumulate and are sent on ``start``. Handlers call
        this from ``__init__`` to apply config-driven settings.

        If the server is already running, call ``send_settings`` to push the
        updated settings.
        """
        self._settings.update(settings)

    async def send_settings(self) -> None:
        """Send current settings to the running LSP server."""
        assert self._session is not None, "LspService not started"
        await self._session.send_notification(
            "workspace/didChangeConfiguration",
            {"settings": self._settings},
        )

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Check a file and return raw LSP diagnostics."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()

        async with self._file_editor.session(
            author=self._file_operation_author
        ) as fe_session:
            async with fe_session.read_file(file_path) as file_info:
                content = file_info.content
                version = file_info.version

        if self._file_versions.get(uri) == version:
            # LSP already has the current content; return cached diagnostics
            return self._diagnostics_data.get(uri, [])

        event = threading.Event()
        self._diagnostics[uri] = event

        lsp_version = self._next_version(uri)
        if uri not in self._open_documents:
            await self._session.send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self._language_id,
                        "version": lsp_version,
                        "text": content,
                    },
                },
            )
            self._open_documents.add(uri)
        else:
            await self._session.send_notification(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": lsp_version},
                    "contentChanges": [{"text": content}],
                },
            )

        self._file_versions[uri] = version

        was_set = await asyncio.to_thread(event.wait, timeout)
        if not was_set:
            self._logger.warning(f"Timeout waiting for LSP diagnostics for {file_path}")
        elif not self._diagnostics_data.get(uri):
            # Got empty initial diagnostics; some servers (e.g. pyrefly) send
            # an empty ack first, then the real diagnostics after analysis.
            # Wait a short settle time for follow-up notifications.
            event.clear()
            await asyncio.to_thread(event.wait, 1.0)

        self._diagnostics.pop(uri, None)

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return self._diagnostics_data.get(uri, [])

    async def format_file(
        self,
        file_path: Path,
        content: str,
        options: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Format a file and return raw LSP TextEdits.

        ``content`` is the file text to format — callers provide it explicitly
        so that the LSP server sees the same content the caller is working with
        (e.g. from the run context after a previous handler already modified it).
        """
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()

        lsp_version = self._next_version(uri)
        if uri not in self._open_documents:
            await self._session.send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self._language_id,
                        "version": lsp_version,
                        "text": content,
                    },
                },
            )
            self._open_documents.add(uri)
        else:
            await self._session.send_notification(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": lsp_version},
                    "contentChanges": [{"text": content}],
                },
            )

        formatting_options = options or {"tabSize": 4, "insertSpaces": True}
        result = await self._session.send_request(
            "textDocument/formatting",
            {
                "textDocument": {"uri": uri},
                "options": formatting_options,
            },
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result or []

    async def _run_event_loop(self, ready: asyncio.Event) -> None:
        async with self._file_editor.session(
            author=self._file_operation_author
        ) as fe_session:
            ready.set()
            async with fe_session.subscribe_to_all_events() as event_iter:
                async for event in event_iter:
                    try:
                        await self._handle_file_event(event)
                    except Exception as exc:
                        self._logger.warning(
                            f"Error forwarding file event to LSP: {exc}"
                        )

    async def _handle_file_event(self, event: ifileeditor.FileEvent) -> None:
        if self._session is None:
            return

        if isinstance(event, ifileeditor.FileOpenEvent):
            uri = event.file_path.as_uri()
            if uri not in self._open_documents:
                try:
                    content = event.file_path.read_text()
                except OSError:
                    return
                lsp_version = self._next_version(uri)
                await self._session.send_notification(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self._language_id,
                            "version": lsp_version,
                            "text": content,
                        },
                    },
                )
                self._open_documents.add(uri)
                self._file_versions[uri] = str(hash(content))

        elif isinstance(event, ifileeditor.FileChangeEvent):
            uri = event.file_path.as_uri()
            lsp_version = self._next_version(uri)
            change = event.change

            if uri not in self._open_documents:
                if isinstance(change, ifileeditor.FileChangeFull):
                    content = change.text
                else:
                    try:
                        content = event.file_path.read_text()
                    except OSError:
                        return
                await self._session.send_notification(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self._language_id,
                            "version": lsp_version,
                            "text": content,
                        },
                    },
                )
                self._open_documents.add(uri)
                self._file_versions[uri] = str(hash(content))
            else:
                if isinstance(change, ifileeditor.FileChangeFull):
                    content_changes = [{"text": change.text}]
                    self._file_versions[uri] = str(hash(change.text))
                else:
                    content_changes = [
                        {
                            "range": {
                                "start": {
                                    "line": change.range.start.line,
                                    "character": change.range.start.character,
                                },
                                "end": {
                                    "line": change.range.end.line,
                                    "character": change.range.end.character,
                                },
                            },
                            "text": change.text,
                        }
                    ]
                    # Partial change: invalidate cached version so check_file
                    # will re-read and send the full updated content next time.
                    self._file_versions.pop(uri, None)
                await self._session.send_notification(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": lsp_version},
                        "contentChanges": content_changes,
                    },
                )

        elif isinstance(event, ifileeditor.FileCloseEvent):
            uri = event.file_path.as_uri()
            if uri in self._open_documents:
                await self._session.send_notification(
                    "textDocument/didClose",
                    {"textDocument": {"uri": uri}},
                )
                self._open_documents.discard(uri)

    def _next_version(self, uri: str) -> int:
        version = self._document_version.get(uri, 0) + 1
        self._document_version[uri] = version
        return version

    async def _handle_configuration_request(
        self, params: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Handle workspace/configuration pull request from the LSP server.

        Returns one copy of the current settings for each requested item.
        """
        items = (params or {}).get("items", [])
        return [self._settings for _ in items] if items else [self._settings]

    async def _handle_diagnostics(self, params: dict[str, Any] | None) -> None:
        if params is None:
            return
        uri = params.get("uri", "")
        diagnostics = params.get("diagnostics", [])
        self._diagnostics_data[uri] = diagnostics

        event = self._diagnostics.get(uri)
        if event is not None:
            event.set()


def map_diagnostics_to_lint_messages(
    raw_diagnostics: list[dict[str, Any]],
    default_source: str = "lsp",
) -> list[lint_files_action.LintMessage]:
    """Convert raw LSP diagnostics to LintMessage objects."""
    severity_map = {
        1: lint_files_action.LintMessageSeverity.ERROR,
        2: lint_files_action.LintMessageSeverity.WARNING,
        3: lint_files_action.LintMessageSeverity.INFO,
        4: lint_files_action.LintMessageSeverity.HINT,
    }

    messages: list[lint_files_action.LintMessage] = []
    for diag in raw_diagnostics:
        rng = diag.get("range", {})
        start = rng.get("start", {})
        end = rng.get("end", {})

        messages.append(
            lint_files_action.LintMessage(
                range=lint_files_action.Range(
                    start=lint_files_action.Position(
                        line=start.get("line", 0),
                        character=start.get("character", 0),
                    ),
                    end=lint_files_action.Position(
                        line=end.get("line", 0),
                        character=end.get("character", 0),
                    ),
                ),
                message=diag.get("message", ""),
                code=str(diag.get("code", ""))
                if diag.get("code") is not None
                else None,
                source=diag.get("source", default_source),
                severity=severity_map.get(diag.get("severity")),
            )
        )
    return messages


def apply_text_edits(content: str, edits: list[dict[str, Any]]) -> str:
    """Apply LSP TextEdits to content and return the new text.

    Edits are applied in reverse order (bottom-to-top) so that earlier
    offsets remain valid after each replacement.
    """
    lines = content.split("\n")

    def offset_of(pos: dict[str, int]) -> int:
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        o = sum(len(lines[i]) + 1 for i in range(min(line, len(lines))))
        if line < len(lines):
            o += min(char, len(lines[line]))
        return o

    sorted_edits = sorted(
        edits,
        key=lambda e: (
            e["range"]["start"]["line"],
            e["range"]["start"]["character"],
        ),
        reverse=True,
    )

    result = content
    for edit in sorted_edits:
        start = offset_of(edit["range"]["start"])
        end = offset_of(edit["range"]["end"])
        result = result[:start] + edit["newText"] + result[end:]

    return result
