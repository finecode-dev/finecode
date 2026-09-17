from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import enum
import sys
import threading
import time
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from finecode_extension_api import service
from finecode_extension_api.interfaces import ifileeditor, ilogger, ilspclient
from finecode_extension_api.resource_uri import resource_uri_to_path

# JSON-RPC "RequestCancelled" code
_REQUEST_CANCELLED_CODE = -32800


class _FileChangeType(enum.IntEnum):
    """`FileChangeType` of ``workspace/didChangeWatchedFiles``.

    The values are the wire codes defined by the LSP specification, not our
    choice, so they are spelled out rather than auto-numbered.
    """

    CREATED = 1
    CHANGED = 2
    DELETED = 3


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

        Whether a server accepts settings *after* it started is that server's own
        business, and some accept none: ruff, for one, reads client settings only
        during ``initialize`` and its ``didChangeConfiguration`` handler does
        nothing, so for it route 2 is decoration and ``send_settings`` cannot take
        effect. Treat the settings a server is started with as final unless that
        server is known to reread them: a service shared by several handlers has to
        collect every handler's settings before the first of them starts it, rather
        than letting whichever handler runs first decide what the others get. See
        ``RuffLspService`` for that shape.

        To push settings to an already running server that does reread them, call
        ``send_settings``.

    Request concurrency:
        By default any number of interactions may be in flight on the session at
        once. A server that does not stay responsive under concurrent access to
        its document state is given a ``max_concurrent_requests`` limit, which
        bounds whole interactions — the document synchronization around a request
        as well as the request itself.
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
        max_concurrent_requests: int | None = None,
        empty_diagnostics_settle_sec: float = 1.0,
    ) -> None:
        self._lsp_client = lsp_client
        self._file_editor = file_editor
        self._logger = logger
        self._cmd = cmd
        self._language_id = language_id
        self._readable_id = readable_id
        self._client_capabilities = client_capabilities
        # PUSH PATH ONLY -- a server offering pull diagnostics never reaches
        # this. How long to keep waiting after a sync's diagnostics come back
        # EMPTY, paid once per clean file. Two distinct things need it, and only
        # the first is about a slow server: a server that acknowledges a
        # document before analyzing it (pyrefly) publishes an empty set first;
        # and a server that clears diagnostics on didClose can have that clear
        # wake the waiter belonging to a later REOPEN of the same document,
        # because publishDiagnostics carries nothing to correlate it with the
        # sync that caused it. Both are guesses this heuristic cannot make
        # reliably, which is the argument for pull diagnostics rather than for
        # a better guess here.
        self._empty_diagnostics_settle_sec = empty_diagnostics_settle_sec
        # LSP leaves parallel request execution to the server's discretion: a
        # server may answer requests concurrently and out of order, or process
        # them strictly one at a time. Nothing in the protocol obliges it to stay
        # responsive with many documents in flight on one session, so how much a
        # given server tolerates is a property of that implementation. None
        # (default) sends without bound, which suits servers that either
        # parallelize properly or queue cleanly; a server whose shared document
        # state degrades under concurrent access passes a limit instead.
        if max_concurrent_requests is not None and max_concurrent_requests < 1:
            raise ValueError(
                "max_concurrent_requests must be >= 1 or None,"
                f" got {max_concurrent_requests}"
            )
        self._request_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrent_requests)
            if max_concurrent_requests is not None
            else None
        )
        self._file_operation_author = ifileeditor.FileOperationAuthor(
            id=readable_id or "LspService"
        )
        self._session: ilspclient.ILspSession | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._start_lock: asyncio.Lock = asyncio.Lock()
        # pending diagnostics waiters: uri -> Events (threading for cross-thread
        # safety). A list, not a single event: one service is shared by every
        # handler in a runner and `_document_leases` exists precisely so several
        # interactions can be in flight on one file at once, so two of them can
        # each be waiting for that file's next diagnostics.
        self._diagnostics: dict[str, list[threading.Event]] = {}
        # last received diagnostics per uri (persistent cache)
        self._diagnostics_data: dict[str, list[dict[str, Any]]] = {}
        # uri -> content version last sent to LSP (for change detection)
        self._file_versions: dict[str, str] = {}
        # uris currently open in the LSP server
        self._open_documents: set[str] = set()
        # uri -> number of interactions currently holding the document open.
        # A feature call closes the document once it is done with it, but "done"
        # is per-interaction and one service is shared by every handler in a
        # runner. Without counting, one call's close lands while another still
        # has a request in flight for that document, and the server answers the
        # second against a document it no longer holds.
        self._document_leases: dict[str, int] = {}
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
        # session root, recorded on start; paths outside it are never named in
        # a watched-file sweep
        self._root_path: Path | None = None
        # whether the server asked to be told about workspace file changes via
        # client/registerCapability; gate `workspace/didChangeWatchedFiles`.
        self._registered_watched_files = False

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
        self._document_leases.clear()
        self._document_version.clear()
        self._uri_locks.clear()
        self._server_capabilities = {}
        self._root_path = None
        self._registered_watched_files = False

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
            initialization_options=(
                {"settings": self._settings} if self._settings else None
            ),
            readable_id=self._readable_id,
            client_capabilities=self._client_capabilities,
        )
        await session.__aenter__()
        self._session = session
        self._root_path = resource_uri_to_path(root_uri)
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
        # Servers may send this whenever their analysis changes, without checking
        # that the client declared workspace.inlayHint.refreshSupport — for some
        # that means once per document sync. Leaving it unhandled costs a warning
        # and a -32601 error response every time.
        self._session.on_request(
            "workspace/inlayHint/refresh",
            self._handle_inlay_hint_refresh,
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

    @contextlib.asynccontextmanager
    async def _request_slot(self) -> collections.abc.AsyncIterator[None]:
        """Bound the work in flight towards the server; a no-op when uncapped.

        A slot covers a whole interaction rather than a single message, because
        what a constrained server has to serialize is access to its document
        state — and notifications mutate that state just as requests read it.

        Not reentrant: at a limit of 1, taking a slot while already holding one
        deadlocks. Code running under a held slot may therefore only send
        notifications directly, never route back through ``request`` or
        ``_send_cancellable_request``. For the same reason a slot is always
        taken *before* a per-uri lock, never the other way round: the two are
        acquired in one order everywhere so they cannot deadlock against each
        other.
        """
        if self._request_semaphore is None:
            yield
            return

        async with self._request_semaphore:
            yield

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> Any:
        """Send an arbitrary LSP request to the running server and return the result.

        Synchronizes nothing. Any request naming a document needs that document
        open on the server, which only `_document_open` arranges — a request sent
        through here reaches whatever the server happens to hold, and for a
        document it does not hold it gets an empty answer rather than an error.
        Feature methods above take the lease for that reason; add one rather than
        reaching for this. Kept for requests that address no document at all.
        """
        assert self._session is not None, "LspService not started"
        async with self._request_slot():
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
        document is already open with this exact content and nothing was sent.

        Both halves of that condition matter. The cached version says what the
        server was last *told*; it says nothing about whether the server still
        holds the document. Skipping on the version alone leaves a closed
        document closed forever — every later request for it is then answered
        against nothing, which for a server with no workspace-wide index (ruff)
        means an empty result and for one with an index (pyrefly) means a
        plausible answer computed from the wrong source of truth.

        Holds the per-uri lock across the whole check-then-send sequence so this
        can't interleave with `_handle_file_event`'s own check-then-send for the
        same uri (see `_uri_locks`).
        """
        assert self._session is not None, "LspService not started"

        content_hash = str(hash(content))
        async with self._get_uri_lock(uri):
            if (
                uri in self._open_documents
                and self._file_versions.get(uri) == content_hash
            ):
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

    @contextlib.asynccontextmanager
    async def _document_open(
        self,
        file_path: Path,
        uri: str,
        content: str,
        *,
        slotted: bool = True,
    ) -> collections.abc.AsyncIterator[bool]:
        """Hold the document open on the server for the duration of the block.

        Yields True if a didOpen/didChange was sent on entry, False if the server
        already had this exact content open — feature methods that wait for a
        server-pushed notification use that to tell "just synced, expect news"
        from "nothing changed, what I have still applies".

        Closes on exit only when this was the last holder and no editor session
        has the file open. Counting holders is what makes it safe for one
        interaction to release the request slot mid-way (see `get_code_actions`)
        and for several handlers to work on one file through the shared service:
        neither can have its document closed by somebody else's cleanup.

        `slotted` covers the sync and the close with a request slot. Callers that
        already hold one for the whole interaction pass False — the slot is not
        reentrant, so taking a second one would deadlock a capped service.
        """
        if slotted:
            async with self._request_slot():
                synced = await self._acquire_lease(uri, content)
        else:
            synced = await self._acquire_lease(uri, content)

        try:
            yield synced
        finally:
            # Also on failure: a request that raised says nothing about whether
            # the server opened the document, and leaving it open grows
            # server-side state that nothing later closes.
            if slotted:
                async with self._request_slot():
                    await self._release_lease(file_path, uri)
            else:
                await self._release_lease(file_path, uri)

    async def _acquire_lease(self, uri: str, content: str) -> bool:
        async with self._get_uri_lock(uri):
            self._document_leases[uri] = self._document_leases.get(uri, 0) + 1
        return await self._sync_document(uri, content)

    async def _release_lease(self, file_path: Path, uri: str) -> None:
        # The slot, when there is one, is taken by the caller: `_request_slot`
        # is always acquired before a uri lock, never inside one, so that the
        # two can't deadlock against each other.
        async with self._get_uri_lock(uri):
            remaining = self._document_leases.get(uri, 1) - 1
            if remaining > 0:
                self._document_leases[uri] = remaining
                return
            self._document_leases.pop(uri, None)
            await self._close_if_not_editor_open(file_path, uri)

    async def _close_if_not_editor_open(self, file_path: Path, uri: str) -> None:
        """Send didClose if no file editor session has the file open.

        Feature methods open documents on demand via _sync_document. Once the
        last of them is done, the LSP server shouldn't keep the document open
        unless an editor session is still actively tracking it.

        Called with the uri lock held, from `_release_lease` only.
        """
        assert self._session is not None, "LspService not started"
        if file_path not in self._file_editor.get_opened_files():
            await self._session.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": uri}},
            )
            self._open_documents.discard(uri)
            # The cached version means "content the server currently has".
            # After a close it has none, and saying otherwise makes the next
            # sync for unchanged content skip the didOpen that would reopen it.
            self._file_versions.pop(uri, None)

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
            async with self._request_slot():
                return await self._session.send_request(method, params, timeout=timeout)
        except Exception as exc:
            if getattr(exc, "code", None) == _REQUEST_CANCELLED_CODE:
                raise ilspclient.LspRequestCancelledError(
                    f"{method} was cancelled by the server, likely because its"
                    " analysis state was invalidated by something elsewhere in"
                    " the workspace"
                ) from exc
            raise

    @contextlib.contextmanager
    def _diagnostics_waiter(
        self, uri: str
    ) -> collections.abc.Iterator[threading.Event]:
        """Register interest in the next diagnostics published for *uri*.

        Entered before the document is synced, never after: the notification can
        arrive between the sync and the wait, and a waiter registered in that gap
        would miss the event it was created for and wait out its whole timeout.
        """
        event = threading.Event()
        self._diagnostics.setdefault(uri, []).append(event)
        try:
            yield event
        finally:
            waiters = self._diagnostics.get(uri)
            if waiters is not None:
                # By identity, and only this one: another interaction on the
                # same file may have registered its own event meanwhile, and
                # clearing the whole uri would take that one with it -- it would
                # then never be set and would wait out its entire timeout.
                for position, registered in enumerate(waiters):
                    if registered is event:
                        del waiters[position]
                        break
                if not waiters:
                    self._diagnostics.pop(uri, None)

    @property
    def _supports_pull_diagnostics(self) -> bool:
        """Whether the server answers ``textDocument/diagnostic``.

        Read off the advertised capability, never assumed: a server without pull
        support does not necessarily reject the request. pyrefly answers it with
        an empty report, which is indistinguishable from a clean file, so
        probing rather than negotiating would silently report every file as
        having no diagnostics.
        """
        return bool(self._server_capabilities.get("diagnosticProvider"))

    @contextlib.contextmanager
    def _diagnostics_interest(
        self, uri: str
    ) -> collections.abc.Iterator[threading.Event | None]:
        """Register for pushed diagnostics, or nothing at all when pulling."""
        if self._supports_pull_diagnostics:
            yield None
            return
        with self._diagnostics_waiter(uri) as event:
            yield event

    async def _pull_diagnostics(self, uri: str, timeout: float) -> list[dict[str, Any]]:
        """Ask the server for this document's diagnostics and return them.

        The document must already be open on the server. A pull naming a
        document the server was never told about is answered with an empty
        report rather than an error, so the sync is what makes the answer
        meaningful -- this must stay inside `_document_open`.
        """
        report = await self._send_cancellable_request(
            "textDocument/diagnostic",
            {"textDocument": {"uri": uri}},
            timeout=timeout,
        )
        if not isinstance(report, dict):
            self._logger.warning(f"Unusable diagnostic report for {uri}: {report!r}")
            return self._diagnostics_data.get(uri, [])
        if report.get("kind") == "unchanged":
            # Only sent in reply to a previousResultId, which this client does
            # not send. Honour it regardless rather than reading "nothing
            # changed" as "no diagnostics".
            return self._diagnostics_data.get(uri, [])
        items = report.get("items") or []
        # Cached under the same key the pushed path uses, so everything reading
        # the last-known diagnostics for a uri keeps working unchanged.
        self._diagnostics_data[uri] = items
        return items

    async def _collect_diagnostics(
        self,
        uri: str,
        event: threading.Event | None,
        document_synced: bool,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """This document's current diagnostics, pulled or pushed.

        Pulling is preferred wherever the server offers it: the answer belongs
        to the request that asked for it, which is the one thing a pushed
        notification cannot tell you. Everything the push path has to guess at
        -- whether a notification is still coming, whether an empty one is a
        clean file or an acknowledgement, whether the one that arrived belongs
        to this sync or to the close before it -- stops being a question.
        """
        if self._supports_pull_diagnostics:
            return await self._pull_diagnostics(uri, timeout)
        assert event is not None, "push path requires a registered waiter"
        return await self._await_diagnostics(uri, event, document_synced, timeout)

    async def _await_diagnostics(
        self,
        uri: str,
        event: threading.Event,
        document_synced: bool,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """Wait for the diagnostics a just-sent sync will produce, and return them.

        The fallback for a server that does not offer pull diagnostics. Prefer
        `_pull_diagnostics`; everything below is inference from an uncorrelated
        notification and is only as good as its guesses.

        Deliberately not under a request slot. Diagnostics arrive as a
        server-sent notification rather than as a response, so holding a slot
        across the wait would serialize whole analyses on a capped server, where
        the constraint is on concurrent access to document state — which the
        sync, not the waiting, is what performs.
        """
        if not document_synced:
            # The server already had this content, so it has already published
            # whatever it has to say about it; there is no new notification coming.
            return self._diagnostics_data.get(uri, [])

        was_set = await asyncio.to_thread(event.wait, timeout)
        if not was_set:
            self._logger.warning(f"Timeout waiting for LSP diagnostics for {uri}")
        elif (
            not self._diagnostics_data.get(uri)
            and self._empty_diagnostics_settle_sec > 0
        ):
            # Got empty initial diagnostics; some servers (e.g. pyrefly) send
            # an empty ack first, then the real diagnostics after analysis.
            # Wait a short settle time for follow-up notifications. A clean file
            # is indistinguishable from an ack here, so this is paid for every
            # clean file -- see `empty_diagnostics_settle_sec`.
            event.clear()
            await asyncio.to_thread(event.wait, self._empty_diagnostics_settle_sec)

        return self._diagnostics_data.get(uri, [])

    async def check_file(
        self,
        file_path: Path,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Check a file and return raw LSP diagnostics."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()

        async with (
            self._file_editor.session(author=self._file_operation_author) as fe_session,
            fe_session.read_file(file_path) as file_info,
        ):
            content = file_info.content

        with self._diagnostics_interest(uri) as event:
            async with self._document_open(file_path, uri, content) as synced:
                return await self._collect_diagnostics(uri, event, synced, timeout)

    async def sync_watched_files(
        self, file_paths: collections.abc.Sequence[Path], recheck_timeout: float
    ) -> set[Path]:
        """Tell a watched-files server these paths may have changed on disk, then
        wait for its recheck to land. Returns the paths that no longer exist."""
        assert self._session is not None, "LspService not started"

        changed: list[tuple[str, _FileChangeType]] = []
        deleted: list[tuple[str, _FileChangeType]] = []
        barrier_uris: list[str] = []
        missing: set[Path] = set()
        for file_path in file_paths:
            uri = file_path.as_uri()
            in_root = self._root_path is not None and file_path.is_relative_to(
                self._root_path
            )
            if not file_path.exists():
                missing.add(file_path)
                if in_root:
                    deleted.append((uri, _FileChangeType.DELETED))
            elif not in_root:
                # Outside the session root the server has no reason to hold the
                # file; it is neither notified nor waited on.
                continue
            elif uri in self._open_documents:
                # The server's buffer for an open document is authoritative, so
                # it is not notified; the recheck triggered by the rest of the
                # batch republishes it, and that republish is what the wait
                # below is for.
                barrier_uris.append(uri)
            else:
                changed.append((uri, _FileChangeType.CHANGED))

        entries = [*changed, *deleted]
        if not entries or not self._registered_watched_files:
            async with self._request_slot():
                for file_path in missing:
                    await self._forget_deleted(file_path.as_uri())
            if not self._registered_watched_files:
                self._logger.debug(
                    "watched-file sweep skipped: server did not register for them"
                )
            else:
                self._logger.debug("watched-file sweep skipped: nothing to notify")
            return missing

        self._logger.debug(
            "watched-file sweep over "
            f"{len(file_paths)} run paths: {len(changed)} type-2, "
            f"{len(deleted)} type-3, {len(barrier_uris)} open run documents, "
            f"{len(missing)} missing"
        )

        with contextlib.ExitStack() as stack:
            # Registered before the notification goes out, per the waiter's
            # rule: the publish cannot land in the gap between the send and the
            # registration. A pull-capable server has no pushed republish to
            # wait for, so it takes the no-signal branch whatever the barrier
            # set contains.
            events = (
                []
                if self._supports_pull_diagnostics
                else [
                    stack.enter_context(self._diagnostics_waiter(uri))
                    for uri in barrier_uris
                ]
            )
            async with self._request_slot():
                for file_path in missing:
                    await self._forget_deleted(file_path.as_uri())
                await self._send_watched_file_changes(entries)

            # The wait deliberately holds no slot: it is a server-side recheck
            # on a background queue, and the recheck's first publish can answer
            # an immediately following sync from the pre-recheck state. Waiting
            # for the recheck to land is what stops that stale-first publish
            # from winning.
            started = time.monotonic()
            if events:
                all_republished = await asyncio.to_thread(
                    _wait_all, events, recheck_timeout
                )
                elapsed_ms = (time.monotonic() - started) * 1000
                if all_republished:
                    self._logger.debug(
                        f"all {len(events)} open run documents republished "
                        f"in {elapsed_ms:.0f} ms"
                    )
                else:
                    overdue = sum(1 for event in events if not event.is_set())
                    self._logger.warning(
                        f"ceiling hit: {overdue} of {len(events)} open run "
                        f"documents did not republish within {recheck_timeout} s"
                    )
            else:
                await asyncio.sleep(recheck_timeout)
                self._logger.debug(
                    f"no open run documents, slept {time.monotonic() - started:.2f} s"
                )
        return missing

    async def get_code_actions(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any],
        *,
        only: list[str] | None = None,
        diagnostic_codes: list[str] | None = None,
        diagnostics_timeout: float = 30.0,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request code actions for a range and return the raw LSP result.

        This document's current diagnostics are put into the request context,
        filtered to those overlapping *range_dict* and, when
        *diagnostic_codes* is given, to those codes. This is what separates
        "here is a fix for this specific problem" from "fix everything in the
        file": a server has no way to offer the former for a diagnostic the
        request never mentioned, and answers an empty context with its blanket
        source actions alone.

        They are passed through exactly as received. Servers identify which
        diagnostic a fix belongs to by data they attached when publishing it
        (ruff carries the whole fix there), so a diagnostic rebuilt from a
        client-side representation matches nothing.

        The whole interaction — sync, diagnostics, request, close — holds one
        document lease, so a concurrent interaction on the same file cannot
        close the document out from under the request.
        """
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()

        with self._diagnostics_interest(uri) as event:
            async with self._document_open(file_path, uri, content) as synced:
                diagnostics = await self._collect_diagnostics(
                    uri, event, synced, diagnostics_timeout
                )

                context: dict[str, Any] = {
                    "diagnostics": _select_diagnostics(
                        diagnostics, range_dict, diagnostic_codes
                    )
                }
                if only is not None:
                    context["only"] = only

                return await self._send_cancellable_request(
                    "textDocument/codeAction",
                    {
                        "textDocument": {"uri": uri},
                        "range": range_dict,
                        "context": context,
                    },
                    timeout=timeout,
                )

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

        # One slot spans the whole open -> request -> close interaction, not just
        # the request: a capped server is being protected from concurrent access
        # to its document state, and the surrounding notifications are part of
        # that state just as much as the request is. The lease takes no slot of
        # its own (`slotted=False`) because this one is already held, and the
        # request goes to the session directly for the same reason.
        async with (
            self._request_slot(),
            self._document_open(file_path, uri, content, slotted=False),
        ):
            formatting_options = options or {"tabSize": 4, "insertSpaces": True}
            result = await self._session.send_request(
                "textDocument/formatting",
                {
                    "textDocument": {"uri": uri},
                    "options": formatting_options,
                },
                timeout=timeout,
            )

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

        async with self._document_open(file_path, uri, content):
            semantic_tokens_provider = self._server_capabilities.get(
                "semanticTokensProvider", {}
            )
            server_supports_range = bool(semantic_tokens_provider.get("range"))
            if range_dict is not None and server_supports_range:
                method = "textDocument/semanticTokens/range"
                params: dict[str, Any] = {
                    "textDocument": {"uri": uri},
                    "range": range_dict,
                }
            else:
                method = "textDocument/semanticTokens/full"
                params = {"textDocument": {"uri": uri}}

            return await self._send_cancellable_request(method, params, timeout=timeout)

    async def get_inlay_hints(
        self,
        file_path: Path,
        content: str,
        range_dict: dict[str, Any],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]] | None:
        """Request inlay hints for a range and return the raw LSP result."""
        assert self._session is not None, "LspService not started"

        uri = file_path.as_uri()
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/inlayHint",
                {"textDocument": {"uri": uri}, "range": range_dict},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/hover",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/definition",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": position,
                    "context": {"includeDeclaration": include_declaration},
                },
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/typeDefinition",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/implementation",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/documentHighlight",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/prepareCallHierarchy",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "callHierarchy/incomingCalls",
                {"item": item},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "callHierarchy/outgoingCalls",
                {"item": item},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "textDocument/prepareTypeHierarchy",
                {"textDocument": {"uri": uri}, "position": position},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "typeHierarchy/supertypes",
                {"item": item},
                timeout=timeout,
            )

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
        async with self._document_open(file_path, uri, content):
            return await self._send_cancellable_request(
                "typeHierarchy/subtypes",
                {"item": item},
                timeout=timeout,
            )

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

        # Forwarded events mutate the same server-side document state that
        # requests read, so on a capped server they belong under a slot too.
        # Taken here, around the per-uri lock rather than inside it, because
        # `_sync_document` acquires the two in that order under an already-held
        # slot; acquiring them in the opposite order here would deadlock.
        async with self._request_slot():
            await self._forward_file_event(event)

    async def _forward_file_event(self, event: ifileeditor.FileEvent) -> None:
        assert self._session is not None

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
                if uri not in self._open_documents:
                    # A document the server does not hold open has no state to
                    # update, and opening one here would add state that nothing
                    # closes again: didClose is only sent for documents an editor
                    # session opened, which this one is not. Written files would
                    # then accumulate in the server for the lifetime of the
                    # session. Dropping the cached hash is enough — the next
                    # feature call re-syncs the document from scratch.
                    self._file_versions.pop(uri, None)
                    return

                lsp_version = self._next_version(uri)
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
                # A held lease outranks the editor closing the tab: dropping the
                # document now would leave an in-flight request to be answered
                # against a document the server no longer holds, which is the
                # failure `_document_leases` exists to prevent -- the editor
                # side is no more entitled to cause it than a handler is. The
                # close is not lost: the last `_release_lease` runs
                # `_close_if_not_editor_open`, and by then the file editor no
                # longer reports the file open, so the didClose goes out there.
                if uri in self._open_documents and uri not in self._document_leases:
                    await self._session.send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                    )
                    self._open_documents.discard(uri)
                    # As in `_close_if_not_editor_open`: the server no longer
                    # holds this document, so the cached version must not claim
                    # it does. A feature call after the user closed the tab has
                    # to reopen it, not skip the sync as unchanged.
                    self._file_versions.pop(uri, None)

        elif isinstance(event, ifileeditor.FileCreateEvent):
            # No didOpen/didChange: a document the server does not hold open has
            # no state to update, and opening one here would add state nothing
            # later closes -- the same leak the `FileChangeEvent` arm documents
            # for a change to an unopened document. A server that registered for
            # watched files still hears about the creation below.
            await self._send_watched_file_change(
                event.file_path.as_uri(), _FileChangeType.CREATED
            )
            return

        elif isinstance(event, ifileeditor.FileDeleteEvent):
            deleted_uri = event.file_path.as_uri()
            await self._forget_deleted(deleted_uri)
            await self._send_watched_file_change(deleted_uri, _FileChangeType.DELETED)

        elif isinstance(event, ifileeditor.FileRenameEvent):
            old_uri = event.old_path.as_uri()
            new_uri = event.new_path.as_uri()
            async with self._get_uri_lock(old_uri):
                if (
                    old_uri in self._open_documents
                    and old_uri not in self._document_leases
                ):
                    await self._session.send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": old_uri}},
                    )
                    self._open_documents.discard(old_uri)
                self._file_versions.pop(old_uri, None)
            # `new_path` needs nothing here. If the server does not hold it
            # open, the next feature call opens it from scratch. If it does
            # hold it open (a rename overwriting a live document), the next
            # feature call's `_sync_document` compares content hashes and sends
            # `didChange` on a mismatch, so the stale document is corrected
            # rather than served -- that is why this arm stays this short.
            await self._send_watched_file_change(old_uri, _FileChangeType.DELETED)
            await self._send_watched_file_change(new_uri, _FileChangeType.CREATED)

    async def _forget_deleted(self, deleted_uri: str) -> None:
        """Drop the server-side state for a path deleted on disk.

        A recursive delete names a directory; the affected documents are the
        directory itself and every uri beneath it. Prefix matching covers both
        without the event having to say which kind of delete it was -- a file
        delete simply has no uri beneath it.
        """
        prefix = deleted_uri + "/"
        affected_open = [
            uri
            for uri in self._open_documents
            if uri == deleted_uri or uri.startswith(prefix)
        ]
        for uri in affected_open:
            async with self._get_uri_lock(uri):
                # A held lease outranks a delete for the same reason it
                # outranks a close: an in-flight request must not be
                # answered against a document the server no longer holds.
                # The cached version is dropped unconditionally -- whatever
                # the server was last told about this path is now about a
                # file that no longer exists.
                if uri not in self._open_documents:
                    continue
                if uri not in self._document_leases:
                    await self._session.send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                    )
                    self._open_documents.discard(uri)
                self._file_versions.pop(uri, None)
        for uri in [
            uri
            for uri in self._file_versions
            if uri == deleted_uri or uri.startswith(prefix)
        ]:
            self._file_versions.pop(uri, None)

    async def _send_watched_file_changes(
        self, changes: list[tuple[str, _FileChangeType]]
    ) -> None:
        """Send one watched-file notification carrying a batch of changes.

        A server that never registered must not receive
        ``workspace/didChangeWatchedFiles``: sending a notification the client
        did not declare support for is a protocol violation. A batch is one
        notification, whichever of its entries survive classification.
        """
        if not changes:
            return
        if not self._registered_watched_files:
            self._logger.debug(
                f"dropped {len(changes)} watched-file changes: "
                "server did not register for them"
            )
            return
        await self._session.send_notification(
            "workspace/didChangeWatchedFiles",
            {
                "changes": [
                    {"uri": uri, "type": change_type.value}
                    for uri, change_type in changes
                ]
            },
        )

    async def _send_watched_file_change(
        self, uri: str, change_type: _FileChangeType
    ) -> None:
        """Notify a server that registered for watched files of a change."""
        await self._send_watched_file_changes([(uri, change_type)])

    def _next_version(self, uri: str) -> int:
        version = self._document_version.get(uri, 0) + 1
        self._document_version[uri] = version
        return version

    async def _handle_register_capability(self, params: dict[str, Any] | None) -> None:
        """Handle client/registerCapability from the LSP server.

        Most registrations are acknowledged without applying any behaviour
        change, because many servers send this even when the client declared
        dynamicRegistration: false for the capability. The one that matters is
        ``workspace/didChangeWatchedFiles``: a server that asks to be told
        about workspace file changes must then actually be told when files are
        created, renamed or deleted.
        """
        registrations = (params or {}).get("registrations", [])
        if any(
            isinstance(registration, dict)
            and registration.get("method") == "workspace/didChangeWatchedFiles"
            for registration in registrations
        ):
            self._registered_watched_files = True
        return

    async def _handle_inlay_hint_refresh(self, params: dict[str, Any] | None) -> None:
        """Handle workspace/inlayHint/refresh from the LSP server.

        Hints are pulled per call and never cached, so there is nothing to
        invalidate. Null is the response the protocol defines for this request,
        and answering it keeps the server from seeing an unhandled method.
        """
        return

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

        # Copy: a waiter's `finally` deregisters from this same list, and it
        # runs on this loop between the awaits of whoever is waiting.
        for event in list(self._diagnostics.get(uri, ())):
            event.set()


def _wait_all(events: list[threading.Event], timeout: float) -> bool:
    """Wait for every event in *events* against one shared deadline.

    Returns True if all were set within *timeout*, False if the budget ran
    out. One call waits on the whole set on a single executor worker rather
    than one worker per event, which is what keeps a run with many open
    documents from occupying every worker in the default executor.
    """
    deadline = time.monotonic() + timeout
    for event in events:
        remaining = deadline - time.monotonic()
        if not event.wait(max(0.0, remaining)):
            return False
    return True


def _lsp_ranges_touch(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True if LSP ranges *a* and *b* overlap or touch at an endpoint.

    Endpoints count as overlapping, which strict interval overlap would reject.
    A code-action request from an editor is commonly a zero-width range at the
    cursor, and under strict comparison a zero-width range overlaps nothing at
    all — the fix for the diagnostic the cursor sits in would never be offered.
    """

    def start(r: dict[str, Any]) -> tuple[int, int]:
        pos = r.get("start", {})
        return (pos.get("line", 0), pos.get("character", 0))

    def end(r: dict[str, Any]) -> tuple[int, int]:
        pos = r.get("end", {})
        return (pos.get("line", 0), pos.get("character", 0))

    return start(a) <= end(b) and start(b) <= end(a)


def _select_diagnostics(
    diagnostics: list[dict[str, Any]],
    range_dict: dict[str, Any],
    codes: list[str] | None,
) -> list[dict[str, Any]]:
    """The diagnostics a code-action request should carry in its context.

    Each is passed on exactly as the server published it — see `get_code_actions`.
    """
    selected = [
        diagnostic
        for diagnostic in diagnostics
        if _lsp_ranges_touch(diagnostic.get("range", {}), range_dict)
    ]
    if codes is not None:
        wanted = set(codes)
        selected = [
            diagnostic
            for diagnostic in selected
            if str(diagnostic.get("code", "")) in wanted
        ]
    return selected


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
