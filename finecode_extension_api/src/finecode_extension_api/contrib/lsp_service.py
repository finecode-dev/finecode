from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, override

from finecode_extension_api import service
from finecode_extension_api.interfaces import ifileeditor, ilogger, ilspclient

# JSON-RPC "RequestCancelled" code
_REQUEST_CANCELLED_CODE = -32800


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
        client_capabilities: dict[str, Any] | None = None,
    ) -> None:
        self._lsp_client = lsp_client
        self._file_editor = file_editor
        self._logger = logger
        self._cmd = cmd
        self._language_id = language_id
        self._readable_id = readable_id
        self._client_capabilities = client_capabilities
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
        # uri -> lock serializing check-then-send-notification sequences, so the
        # event-forwarding loop (_handle_file_event) and direct handler calls
        # (_sync_document, e.g. from get_hover) can't both observe "not synced yet"
        # for the same uri and each send their own didOpen/didChange. Without this,
        # one notification can land on the wire after the other call's request is
        # already in flight, and the server cancels it as a "subsequent mutation".
        self._uri_locks: dict[str, asyncio.Lock] = {}
        # LSP protocol version counter per uri
        self._document_version: dict[str, int] = {}
        # current settings, accumulated via update_settings and sent on start
        self._settings: dict[str, Any] = {}
        # server capabilities populated once after the initialize handshake
        self._server_capabilities: dict[str, Any] = {}

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
        self._uri_locks.clear()
        self._server_capabilities = {}

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
            client_capabilities=self._client_capabilities,
        )
        await session.__aenter__()
        self._session = session
        self._server_capabilities = session.server_capabilities
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
        # Some LSP servers send client/registerCapability regardless of whether
        # we declared dynamicRegistration: false for individual capabilities.
        # Returning null (None) is the correct LSP response: we acknowledge the
        # registration silently and apply no behaviour change.
        self._session.on_request(
            "client/registerCapability",
            self._handle_register_capability,
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

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> Any:
        """Send an arbitrary LSP request to the running server and return the result."""
        assert self._session is not None, "LspService not started"
        return await self._session.send_request(method, params, timeout=timeout)

    async def _sync_document(self, uri: str, content: str) -> bool:
        """Send didOpen/didChange only if content differs from what the server last saw.

        Every LSP feature call used to send a didChange unconditionally, bumping the
        document version even when content was unchanged. A concurrent call (e.g. a
        diagnostics run overlapping a hover) would then see a "changed" document and
        emit its own didChange, which servers like pyrefly treat as a real mutation
        and use it to cancel any older in-flight request for that document — turning
        two harmless concurrent reads into a spurious cancellation error. Gating on
        content identity here keeps notifications limited to actual changes.

        Returns True if a didOpen/didChange notification was sent, False if the
        cached content already matched and nothing was sent.

        Holds the per-uri lock across the whole check-then-send sequence so this
        can't interleave with `_handle_file_event`'s own check-then-send for the
        same uri (see `_uri_locks`).
        """
        assert self._session is not None, "LspService not started"

        content_hash = str(hash(content))
        async with self._get_uri_lock(uri):
            if self._file_versions.get(uri) == content_hash:
                return False

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
            self._file_versions[uri] = content_hash
            return True

    def _get_uri_lock(self, uri: str) -> asyncio.Lock:
        lock = self._uri_locks.get(uri)
        if lock is None:
            lock = asyncio.Lock()
            self._uri_locks[uri] = lock
        return lock

    async def _send_cancellable_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> Any:
        """Send a request; translate a server-side cancellation into LspRequestCancelledError.

        LSP servers with a global analysis snapshot (e.g. pyrefly, like
        rust-analyzer) can cancel an in-flight request whenever something
        elsewhere in the workspace invalidates that snapshot — most commonly
        a document mutating, though the exact trigger is server-specific and
        not something this client observes directly. The concrete transport
        raises an exception carrying ``code == -32800`` for this. Detected
        via duck typing (``getattr(exc, "code", None)``) rather than
        ``isinstance`` because this module must not depend on the concrete
        JSON-RPC transport package.
        """
        assert self._session is not None, "LspService not started"
        try:
            return await self._session.send_request(method, params, timeout=timeout)
        except Exception as exc:
            if getattr(exc, "code", None) == _REQUEST_CANCELLED_CODE:
                raise ilspclient.LspRequestCancelledError(
                    f"{method} was cancelled by the server, likely because its"
                    " analysis state was invalidated by something elsewhere in"
                    " the workspace"
                ) from exc
            raise

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

        event = threading.Event()
        self._diagnostics[uri] = event

        if not await self._sync_document(uri, content):
            # LSP already has the current content; return cached diagnostics
            self._diagnostics.pop(uri, None)
            return self._diagnostics_data.get(uri, [])

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

        await self._sync_document(uri, content)

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

    async def get_semantic_tokens(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Request semantic tokens for a file and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()

        await self._sync_document(uri, content)

        semantic_tokens_provider = self._server_capabilities.get(
            "semanticTokensProvider", {}
        )
        server_supports_range = bool(semantic_tokens_provider.get("range"))
        if range_dict is not None and server_supports_range:
            method = "textDocument/semanticTokens/range"
            params: dict[str, Any] = {"textDocument": {"uri": uri}, "range": range_dict}
        else:
            method = "textDocument/semanticTokens/full"
            params = {"textDocument": {"uri": uri}}

        result = await self._send_cancellable_request(method, params, timeout=timeout)

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_hover(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Request hover information for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Request definition location(s) for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_references(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        include_declaration: bool = True,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request reference locations for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": position,
                "context": {"includeDeclaration": include_declaration},
            },
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_type_definition(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Request type definition location(s) for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/typeDefinition",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_implementation(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        """Request implementation location(s) for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/implementation",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_document_highlight(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request document highlights for a position and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/documentHighlight",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_call_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request call hierarchy preparation and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/prepareCallHierarchy",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_call_hierarchy_incoming_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request incoming calls for a call hierarchy item and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "callHierarchy/incomingCalls",
            {"item": item},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_call_hierarchy_outgoing_calls(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request outgoing calls for a call hierarchy item and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "callHierarchy/outgoingCalls",
            {"item": item},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_type_hierarchy_prepare(
        self,
        file_path: Path,
        content: str,
        position: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request type hierarchy preparation and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "textDocument/prepareTypeHierarchy",
            {"textDocument": {"uri": uri}, "position": position},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_type_hierarchy_supertypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request supertypes for a type hierarchy item and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "typeHierarchy/supertypes",
            {"item": item},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

    async def get_type_hierarchy_subtypes(
        self,
        file_path: Path,
        content: str,
        item: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request subtypes for a type hierarchy item and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        await self._sync_document(uri, content)

        result = await self._send_cancellable_request(
            "typeHierarchy/subtypes",
            {"item": item},
            timeout=timeout,
        )

        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)

        return result

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
            async with self._get_uri_lock(uri):
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
            change = event.change

            async with self._get_uri_lock(uri):
                lsp_version = self._next_version(uri)
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
            async with self._get_uri_lock(uri):
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

    async def _handle_register_capability(
        self, params: dict[str, Any] | None
    ) -> None:
        """Handle client/registerCapability from the LSP server.

        Many servers send this even when the client declared dynamicRegistration:
        false for specific capabilities. Returning null (None) acknowledges the
        registration per LSP spec without actually applying any behaviour change.
        """
        return None

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


