from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from finecode_extension_api.contrib.lsp_service import LspService
from finecode_extension_api.interfaces import ifileeditor, ilspclient


@dataclasses.dataclass
class _SentNotification:
    method: str
    params: dict[str, Any] | None


@dataclasses.dataclass
class _SentRequest:
    method: str
    params: dict[str, Any] | None


class _FakeLspSession:
    """Records notifications instead of talking to a real language server."""

    def __init__(self) -> None:
        self.notifications: list[_SentNotification] = []
        self.requests: list[_SentRequest] = []
        # Every message in the order it went out, so a test can assert that one
        # thing happened before another rather than merely that both happened.
        self.traffic: list[str] = []
        # Canned results per request method.
        self.request_results: dict[str, Any] = {}
        # Called with the uri whenever a document is synced, standing in for a
        # server that publishes diagnostics after each open or change.
        self.on_document_synced: Any = None
        # When set, send_notification blocks until this event fires, letting a
        # test force two concurrent syncs to interleave at a specific point.
        self.release_send: asyncio.Event | None = None
        # When set, send_request raises this instead of its normal behavior —
        # lets a test simulate a transport-level error (e.g. a server-side
        # cancellation) without a real LSP server.
        self.raise_on_send_request: Exception | None = None
        # When set, send_request blocks until this event fires, so a test can
        # hold one request open and observe whether a second is allowed to start.
        self.release_request: asyncio.Event | None = None
        # Requests currently being served, and the high-water mark over the
        # session — how a test observes a concurrency limit taking effect.
        self.in_flight = 0
        self.max_in_flight = 0
        # What the server advertised in its initialize result. Declaring
        # `diagnosticProvider` here is what puts the service on the pull path.
        self.capabilities: dict[str, Any] = {}

    async def __aenter__(self) -> "_FakeLspSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.requests.append(_SentRequest(method, params))
        self.traffic.append(method)
        try:
            if self.raise_on_send_request is not None:
                raise self.raise_on_send_request
            if self.release_request is not None:
                await self.release_request.wait()
            return self.request_results.get(method)
        finally:
            self.in_flight -= 1

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if self.release_send is not None:
            await self.release_send.wait()
        self.notifications.append(_SentNotification(method, params))
        self.traffic.append(method)
        if self.on_document_synced is not None and method in (
            "textDocument/didOpen",
            "textDocument/didChange",
        ):
            uri = (params or {}).get("textDocument", {}).get("uri", "")
            await self.on_document_synced(uri)

    def on_notification(self, method: str, handler: Any) -> None:
        pass

    def on_request(self, method: str, handler: Any) -> None:
        pass

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self.capabilities

    @property
    def server_info(self) -> dict[str, Any] | None:
        return None

    def sync_notification_count(self, uri: str) -> int:
        """Count didOpen/didChange notifications sent for a document."""
        return sum(
            1
            for n in self.notifications
            if n.method in ("textDocument/didOpen", "textDocument/didChange")
            and n.params is not None
            and n.params.get("textDocument", {}).get("uri") == uri
        )

    def notification_count(self, method: str, uri: str) -> int:
        return sum(
            1
            for n in self.notifications
            if n.method == method
            and n.params is not None
            and n.params.get("textDocument", {}).get("uri") == uri
        )

    def only_request(self, method: str) -> _SentRequest:
        matching = [r for r in self.requests if r.method == method]
        assert len(matching) == 1, f"expected exactly one {method}, got {len(matching)}"
        return matching[0]


class _FakeLspClient:
    def __init__(self, session: _FakeLspSession) -> None:
        self._session = session

    def session(self, **kwargs: Any) -> _FakeLspSession:
        return self._session


class _FakeFileEditorSession:
    def __init__(
        self, content: str, events: asyncio.Queue[ifileeditor.FileEvent]
    ) -> None:
        self._content = content
        self._events = events

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: Path, block: bool = False
    ) -> AsyncIterator[ifileeditor.FileInfo]:
        yield ifileeditor.FileInfo(content=self._content, version="1")

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(self) -> AsyncIterator[Any]:
        async def _drain_queue() -> AsyncIterator[Any]:
            # Stays pending forever while the queue is empty, so tests that don't
            # exercise event forwarding see the subscription simply never yield.
            while True:
                yield await self._events.get()

        yield _drain_queue()


class _FakeFileEditor:
    """Reports the subject file as open in the IDE, so the LSP session for it
    stays open across calls instead of being closed after every request —
    matching the common case where a hover fires while the user has the file
    open for editing.
    """

    def __init__(self, file_path: Path, content: str) -> None:
        self.file_path = file_path
        self.content = content
        self.events: asyncio.Queue[ifileeditor.FileEvent] = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def session(self, author: Any) -> AsyncIterator[_FakeFileEditorSession]:
        yield _FakeFileEditorSession(self.content, self.events)

    def get_opened_files(self) -> list[Path]:
        return [self.file_path]


class _NullLogger:
    def exception(self, exception: Exception) -> None:
        pass

    def trace(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def disable(self, package: str) -> None:
        pass

    def enable(self, package: str) -> None:
        pass


@contextlib.asynccontextmanager
async def _running_service(
    file_path: Path,
    content: str,
    max_concurrent_requests: int | None = None,
    server_capabilities: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[LspService, _FakeLspSession, _FakeFileEditor]]:
    session = _FakeLspSession()
    if server_capabilities is not None:
        session.capabilities = server_capabilities
    file_editor = _FakeFileEditor(file_path, content)
    service = LspService(
        lsp_client=_FakeLspClient(session),
        file_editor=file_editor,  # type: ignore[arg-type]
        logger=_NullLogger(),  # type: ignore[arg-type]
        cmd="fake-lsp-server",
        language_id="python",
        max_concurrent_requests=max_concurrent_requests,
    )
    await service.ensure_started(root_uri=file_path.parent.as_uri())
    try:
        yield service, session, file_editor
    finally:
        await service._async_dispose()


async def test_repeated_lsp_feature_calls_on_unchanged_file_do_not_resync(
    tmp_path: Path,
) -> None:
    """A second feature request for the same, unmodified file must not re-sync the document.

    Before this behavior, every feature request re-sent the document to the
    language server even when nothing had changed. Two such requests racing on
    the same open file (e.g. a hover firing while diagnostics are still being
    computed) would each look like an edit to the server, which then cancels
    the older in-flight request — surfacing as an unhandled LSP error to the
    user for something that was never actually edited.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"

    async with _running_service(file_path, content) as (service, session, _):
        await service.get_hover(file_path, content, {"line": 0, "character": 0})
        await service.get_hover(file_path, content, {"line": 0, "character": 0})

        assert session.sync_notification_count(file_path.as_uri()) == 1


async def test_diagnostics_and_hover_on_unchanged_file_share_one_sync(
    tmp_path: Path,
) -> None:
    """Diagnostics and a hover on the same unmodified, still-open file must not each sync it independently.

    This is the exact shape of the original bug report: opening a file in the
    IDE commonly triggers both a diagnostics check and a hover in quick
    succession. If each synced the document on its own, the second sync would
    look like a real edit to the language server and cancel the first request
    outright.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"

    async with _running_service(file_path, content) as (service, session, _):
        await service.check_file(file_path, timeout=0.05)
        await service.get_hover(file_path, content, {"line": 0, "character": 0})

        assert session.sync_notification_count(file_path.as_uri()) == 1


async def test_lsp_feature_call_resyncs_after_file_content_changes(
    tmp_path: Path,
) -> None:
    """A feature request must still pick up new content once the file actually changes.

    Guards against over-caching: skipping redundant syncs for unchanged content
    must not also skip syncs when the content genuinely changed, which would
    make the server analyze stale code.
    """
    file_path = tmp_path / "subject.py"

    async with _running_service(file_path, "x = 1\n") as (service, session, _):
        await service.get_hover(file_path, "x = 1\n", {"line": 0, "character": 0})
        await service.get_hover(file_path, "x = 2\n", {"line": 0, "character": 0})

        assert session.sync_notification_count(file_path.as_uri()) == 2


async def test_file_open_event_and_hover_race_do_not_double_sync(
    tmp_path: Path,
) -> None:
    """A file-open event forwarded from the IDE and a hover request racing on the
    same file must not each independently sync the document.

    ``_handle_file_event`` (the event-forwarding loop) and ``get_hover`` (a direct
    handler call, via ``_sync_document``) both decide whether a document needs a
    fresh didOpen/didChange by checking then updating shared state. If that
    check-then-act isn't atomic across the two call paths, both can observe
    "not synced yet" for the same uri and each send their own notification —
    turning a harmless race into two didOpen calls, the second of which the
    language server treats as an edit to a document with an in-flight request
    and cancels it. This is the exact shape of the original bug report: opening
    a file in the IDE and immediately hovering over it.

    The race is in the shared ``_sync_document``/``_handle_file_event`` path, so
    it applies to every LSP feature method (``get_definition``, ``get_references``,
    ``get_call_hierarchy_prepare``, etc.), not just hover — hover is exercised
    here as one representative call site, not because it's special-cased.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"
    file_path.write_text(content)

    async with _running_service(file_path, content) as (service, session, _):
        release = asyncio.Event()
        session.release_send = release

        open_event_task = asyncio.create_task(
            service._handle_file_event(ifileeditor.FileOpenEvent(file_path=file_path))
        )
        # Let the open-event handler reach send_notification and block there,
        # still holding the uri lock (if the fix is in place).
        await asyncio.sleep(0)

        hover_task = asyncio.create_task(
            service.get_hover(file_path, content, {"line": 0, "character": 0})
        )
        # Let the hover call attempt its own sync — with the fix, it blocks
        # trying to acquire the same uri lock instead of racing ahead.
        await asyncio.sleep(0)

        release.set()
        await open_event_task
        await hover_task

        assert session.sync_notification_count(file_path.as_uri()) == 1


class _FakeTransportError(Exception):
    """Duck-typed shape of a transport-level JSON-RPC error carrying a code,
    without depending on finecode_jsonrpc (which this package must not
    import).
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def test_server_cancellation_is_translated_to_lsp_request_cancelled_error(
    tmp_path: Path,
) -> None:
    """A transport error carrying code -32800 (RequestCancelled) must be
    translated into ilspclient.LspRequestCancelledError, not left as the raw
    transport exception or swallowed.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"

    async with _running_service(file_path, content) as (service, session, _):
        session.raise_on_send_request = _FakeTransportError(-32800, "cancelled")

        with pytest.raises(ilspclient.LspRequestCancelledError):
            await service.get_hover(file_path, content, {"line": 0, "character": 0})


async def test_unrelated_send_request_error_is_not_mistranslated(
    tmp_path: Path,
) -> None:
    """A transport error without a -32800 code (or without a code attribute at
    all) must propagate unchanged — it must not be swallowed or mistranslated
    into LspRequestCancelledError.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"

    async with _running_service(file_path, content) as (service, session, _):
        session.raise_on_send_request = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await service.get_hover(file_path, content, {"line": 0, "character": 0})


async def test_capped_service_serializes_interactions_for_different_files(
    tmp_path: Path,
) -> None:
    """With a limit of 1, a second file's interaction waits for the first.

    LSP leaves parallel request execution to the server, and a server whose
    shared document state degrades under concurrent access needs the client to
    stop pipelining. The limit has to hold across the whole interaction, not
    just the request, or another file's synchronization still interleaves.
    """
    file_a = tmp_path / "a.toml"
    file_b = tmp_path / "b.toml"

    async with _running_service(file_a, "x = 1\n", max_concurrent_requests=1) as (
        service,
        session,
        _,
    ):
        session.release_request = asyncio.Event()
        tasks = [
            asyncio.create_task(service.format_file(file_a, "x = 1\n")),
            asyncio.create_task(service.format_file(file_b, "y = 2\n")),
        ]
        # Long enough for both tasks to reach the server or queue behind the limit.
        await asyncio.sleep(0.05)
        assert session.in_flight == 1

        session.release_request.set()
        await asyncio.gather(*tasks)

        assert session.max_in_flight == 1


async def test_uncapped_service_leaves_interactions_concurrent(
    tmp_path: Path,
) -> None:
    """The default must keep sending without bound.

    Servers that parallelize properly are slowed down by serialization for no
    benefit, so the limit is opt-in per server.
    """
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"

    async with _running_service(file_a, "x = 1\n") as (service, session, _):
        session.release_request = asyncio.Event()
        tasks = [
            asyncio.create_task(service.format_file(file_a, "x = 1\n")),
            asyncio.create_task(service.format_file(file_b, "y = 2\n")),
        ]
        await asyncio.sleep(0.05)
        assert session.in_flight == 2

        session.release_request.set()
        await asyncio.gather(*tasks)


async def test_change_to_document_the_server_does_not_hold_open_is_not_opened(
    tmp_path: Path,
) -> None:
    """A change to a closed document must not open it in the server.

    didClose is only sent for documents an editor session opened, so a document
    opened in response to a write would stay open for the rest of the session.
    Every file written through the editor would accumulate that way — for a
    server whose document state is contended, unboundedly so. The cached
    content hash is dropped instead, leaving the next feature call to re-sync.
    """
    subject = tmp_path / "subject.py"
    written = tmp_path / "written_by_a_handler.py"

    async with _running_service(subject, "x = 1\n") as (service, session, file_editor):
        await file_editor.events.put(
            ifileeditor.FileChangeEvent(
                file_path=written,
                author=ifileeditor.FileOperationAuthor(id="some-handler"),
                change=ifileeditor.FileChangeFull(text="y = 2\n"),
            )
        )
        # Long enough for the forwarding loop to consume the event.
        await asyncio.sleep(0.05)

        assert session.sync_notification_count(written.as_uri()) == 0

        # The next feature call still sends the current content, so dropping the
        # notification costs no correctness.
        await service.get_hover(written, "y = 2\n", {"line": 0, "character": 0})
        assert session.sync_notification_count(written.as_uri()) == 1


_F541 = {
    "range": {
        "start": {"line": 1, "character": 8},
        "end": {"line": 1, "character": 24},
    },
    "code": "F541",
    "message": "f-string without any placeholders",
    # Servers identify which diagnostic a fix belongs to by what they attached
    # here when publishing it, so this is the field a client must not drop.
    "data": {"fix": {"edits": ["..."]}, "kind": "F541"},
}
_F401 = {
    "range": {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 9},
    },
    "code": "F401",
    "message": "`os` imported but unused",
    "data": {"fix": {"edits": ["..."]}, "kind": "F401"},
}

_WHOLE_FILE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 2, "character": 0},
}


@contextlib.asynccontextmanager
async def _service_publishing(
    subject: Path, published: list[dict[str, Any]]
) -> AsyncIterator[tuple[LspService, _FakeLspSession, Path]]:
    """A service whose server publishes *published* every time a document syncs.

    The yielded path is a file **no editor session holds open** — the case where
    documents are opened on demand and closed again, i.e. every caller that isn't
    an IDE with a live buffer: MCP, the CLI, CI.
    """
    editor_open_file = subject.parent / "held_open_by_the_editor.py"
    async with _running_service(editor_open_file, "x = 1\n") as (
        service,
        session,
        _,
    ):

        async def publish(uri: str) -> None:
            await service._handle_diagnostics({"uri": uri, "diagnostics": published})

        session.on_document_synced = publish
        yield service, session, subject


async def test_document_closed_after_a_request_is_reopened_for_the_next_one(
    tmp_path: Path,
) -> None:
    """A second request for an unchanged file must reopen it, not assume it is still open.

    Documents opened on demand are closed again once the call that opened them is
    done, and the record of what the server was last told outlives that close. If
    a later call treats that record as proof the server still holds the document,
    it sends its request against nothing: empty results forever from any caller
    without an editor behind it, on every file already looked at once.
    """
    subject = tmp_path / "subject.py"

    async with _service_publishing(subject, []) as (service, session, subject):
        await service.get_hover(subject, "x = 1\n", {"line": 0, "character": 0})
        assert (
            session.notification_count("textDocument/didClose", subject.as_uri()) == 1
        )

        await service.get_hover(subject, "x = 1\n", {"line": 0, "character": 0})

        assert session.sync_notification_count(subject.as_uri()) == 2


async def test_overlapping_requests_on_one_file_share_a_single_open(
    tmp_path: Path,
) -> None:
    """One call finishing must not close a document another call is still using.

    One service instance is shared by every handler in a runner, so two of them
    can be working on the same file at once. If each closes the document when its
    own request returns, the first to finish pulls the document out from under
    the other, which then gets an answer computed against a document the server
    no longer holds — intermittently, and only under concurrency.
    """
    subject = tmp_path / "subject.py"

    async with _service_publishing(subject, []) as (service, session, subject):
        session.release_request = asyncio.Event()
        tasks = [
            asyncio.create_task(
                service.get_hover(subject, "x = 1\n", {"line": 0, "character": 0})
            ),
            asyncio.create_task(
                service.get_definition(subject, "x = 1\n", {"line": 0, "character": 0})
            ),
        ]
        # Long enough for both to have synced and be waiting on their requests.
        await asyncio.sleep(0.05)
        assert (
            session.notification_count("textDocument/didClose", subject.as_uri()) == 0
        )

        session.release_request.set()
        await asyncio.gather(*tasks)

        assert session.notification_count("textDocument/didOpen", subject.as_uri()) == 1
        assert (
            session.notification_count("textDocument/didClose", subject.as_uri()) == 1
        )
        assert session.traffic.index("textDocument/didClose") > session.traffic.index(
            "textDocument/definition"
        )


async def test_code_actions_are_requested_while_the_document_is_open(
    tmp_path: Path,
) -> None:
    """The code-action request must reach the server before the document is closed.

    Fetching a file's diagnostics and asking what fixes them are two steps, and a
    document opened for the first is of no use to the second if it is closed in
    between: a server asked for actions on a document it does not hold has
    nothing to answer with and returns none. Every fix request from a caller
    without an open editor behind it then comes back empty.
    """
    subject = tmp_path / "subject.py"

    async with _service_publishing(subject, [_F541]) as (service, session, subject):
        await service.get_code_actions(subject, "x = 1\n", _WHOLE_FILE)

        traffic = session.traffic
        assert traffic.index("textDocument/didOpen") < traffic.index(
            "textDocument/codeAction"
        )
        assert traffic.index("textDocument/codeAction") < traffic.index(
            "textDocument/didClose"
        )


async def test_code_action_request_carries_the_published_diagnostics(
    tmp_path: Path,
) -> None:
    """A code-action request must tell the server which diagnostics it is about.

    A server can only offer "fix this specific problem" for a diagnostic the
    request names; asked about none, it answers with whatever blanket source
    actions it has ("fix everything in the file") and no targeted fix at all.
    Diagnostics go back exactly as they arrived because servers recognise them by
    data they attached when publishing — one rebuilt from a client-side
    representation matches nothing, which looks identical to sending none.
    """
    subject = tmp_path / "subject.py"

    async with _service_publishing(subject, [_F541]) as (service, session, subject):
        await service.get_code_actions(subject, "x = 1\n", _WHOLE_FILE)

        request = session.only_request("textDocument/codeAction")
        assert request.params is not None
        assert request.params["context"]["diagnostics"] == [_F541]


async def test_code_action_context_can_be_narrowed_to_specific_codes(
    tmp_path: Path,
) -> None:
    """Asking for one diagnostic's fixes must not offer to fix the rest of the file.

    A caller fixing one problem — an agent applying a single fix, an IDE quick-fix
    menu on one squiggle — gets whatever the request's diagnostics allow. Passing
    all of the file's diagnostics would return fixes for problems the user never
    pointed at, with no way to tell afterwards which fix belonged to which.
    """
    subject = tmp_path / "subject.py"

    async with _service_publishing(subject, [_F401, _F541]) as (
        service,
        session,
        subject,
    ):
        await service.get_code_actions(
            subject, "x = 1\n", _WHOLE_FILE, diagnostic_codes=["F541"]
        )

        request = session.only_request("textDocument/codeAction")
        assert request.params is not None
        assert request.params["context"]["diagnostics"] == [_F541]


async def test_code_action_context_includes_the_diagnostic_under_the_cursor(
    tmp_path: Path,
) -> None:
    """A request at a bare cursor position must still find the diagnostic it sits in.

    An editor asks for code actions at the caret, which is an empty range. Under
    strict overlap an empty range intersects nothing at all, so the one case that
    matters most — the user's cursor inside a squiggle — would offer no fix.
    """
    subject = tmp_path / "subject.py"
    # At the diagnostic's first character, where an empty range and the
    # diagnostic share exactly one endpoint and nothing more — a caret one
    # column further in would be matched by strict overlap too, and would not
    # tell the two rules apart.
    caret = {
        "start": {"line": 1, "character": 8},
        "end": {"line": 1, "character": 8},
    }

    async with _service_publishing(subject, [_F401, _F541]) as (
        service,
        session,
        subject,
    ):
        await service.get_code_actions(subject, "x = 1\n", caret)

        request = session.only_request("textDocument/codeAction")
        assert request.params is not None
        assert request.params["context"]["diagnostics"] == [_F541]


def test_concurrency_limit_below_one_is_rejected() -> None:
    """A limit of 0 would block every request forever; fail at construction."""
    file_editor = _FakeFileEditor(Path("/nonexistent"), "")
    with pytest.raises(ValueError, match="max_concurrent_requests"):
        LspService(
            lsp_client=_FakeLspClient(_FakeLspSession()),
            file_editor=file_editor,  # type: ignore[arg-type]
            logger=_NullLogger(),  # type: ignore[arg-type]
            cmd="fake-lsp-server",
            language_id="python",
            max_concurrent_requests=0,
        )


async def test_two_waiters_for_one_file_are_both_woken_by_its_diagnostics(
    tmp_path: Path,
) -> None:
    """Diagnostics interest is per-interaction, not per-file.

    One service is shared by every handler in a runner, and ``_document_leases``
    exists precisely so two interactions can be in flight on one file at once —
    a lint check and a code-action request, say. Registering interest under a
    single slot per uri meant the second registration replaced the first, and
    whichever finished first deregistered the *other* one's event: the survivor
    was never woken and sat out its whole 30s diagnostics timeout.
    """
    file_path = tmp_path / "subject.py"
    uri = file_path.as_uri()

    async with _running_service(file_path, "x = 1\n") as (service, _, _):
        with service._diagnostics_waiter(uri) as first:
            with service._diagnostics_waiter(uri) as second:
                assert first is not second

                await service._handle_diagnostics({"uri": uri, "diagnostics": []})

                assert first.is_set()
                assert second.is_set()

            # Leaving the inner block must deregister only its own event.
            first.clear()
            await service._handle_diagnostics({"uri": uri, "diagnostics": []})
            assert first.is_set()

        assert uri not in service._diagnostics


async def test_editor_closing_a_tab_does_not_close_a_document_still_in_use(
    tmp_path: Path,
) -> None:
    """A file-close event from the IDE must not drop a document a request is using.

    ``_document_leases`` stops one handler's cleanup from closing a document
    another handler still needs, but the close event forwarded when the user
    closes the tab went straight to didClose regardless — the same failure,
    reached from the editor side instead. The close is not lost: the last lease
    release closes the document once the file editor no longer reports it open.
    """
    file_path = tmp_path / "subject.py"
    content = "x = 1\n"
    uri = file_path.as_uri()

    async with _running_service(file_path, content) as (service, session, file_editor):
        release = asyncio.Event()
        session.release_request = release

        hover_task = asyncio.create_task(
            service.get_hover(file_path, content, {"line": 0, "character": 0})
        )
        # Long enough for the hover to have synced and be waiting on its request,
        # holding the document's lease.
        await asyncio.sleep(0.05)
        assert service._document_leases.get(uri) == 1

        # The user closes the tab while that request is still in flight.
        file_editor.get_opened_files = lambda: []  # type: ignore[method-assign]
        await service._handle_file_event(
            ifileeditor.FileCloseEvent(
                file_path=file_path,
                author=ifileeditor.FileOperationAuthor(id="editor"),
            )
        )

        assert session.notification_count("textDocument/didClose", uri) == 0

        release.set()
        await hover_task

        # Released now that nothing holds it and no editor session has it open.
        assert session.notification_count("textDocument/didClose", uri) == 1


_PULL_CAPABLE = {"diagnosticProvider": {"interFileDependencies": False}}


async def test_a_pull_capable_server_is_asked_for_diagnostics_not_waited_on(
    tmp_path: Path,
) -> None:
    """When the server offers pull diagnostics, ask it.

    A pushed notification carries nothing tying it to the sync that caused it,
    so the push path has to infer: whether another notification is still coming,
    whether an empty one means a clean file or an acknowledgement, whether the
    one that arrived belongs to this sync or to the close before it. A pull is
    a request with a reply — none of those are questions any more.
    """
    file_path = tmp_path / "subject.py"
    uri = file_path.as_uri()

    async with _running_service(
        file_path, "x = 1\n", server_capabilities=_PULL_CAPABLE
    ) as (service, session, _):
        session.request_results["textDocument/diagnostic"] = {
            "kind": "full",
            "items": [_F401],
        }

        diagnostics = await service.check_file(file_path)

        assert diagnostics == [_F401]
        assert session.only_request("textDocument/diagnostic").params == {
            "textDocument": {"uri": uri}
        }
        # Nothing registered interest in a notification that is not coming.
        assert uri not in service._diagnostics


async def test_a_server_without_pull_support_still_waits_for_pushed_diagnostics(
    tmp_path: Path,
) -> None:
    """Pull is negotiated, never assumed.

    A server that does not advertise `diagnosticProvider` may still answer
    `textDocument/diagnostic` — pyrefly replies with an empty report, which is
    indistinguishable from a clean file. Probing instead of reading the
    advertised capability would report every file as having no diagnostics.
    """
    file_path = tmp_path / "subject.py"

    async with _service_publishing(file_path, [_F401]) as (service, session, subject):
        diagnostics = await service.check_file(subject, timeout=0.05)

        assert diagnostics == [_F401]
        assert not [
            r for r in session.requests if r.method == "textDocument/diagnostic"
        ]


async def test_pulled_diagnostics_reach_the_code_action_context(
    tmp_path: Path,
) -> None:
    """The code-action context must be filled the same way on either path.

    Servers match a fix to a diagnostic by data they attached to it, so a
    code-action request whose context omits the diagnostic gets the blanket
    source actions and nothing specific. Switching how diagnostics are obtained
    must not quietly empty that context.
    """
    file_path = tmp_path / "subject.py"

    async with _running_service(
        file_path, "import os\n", server_capabilities=_PULL_CAPABLE
    ) as (service, session, _):
        session.request_results["textDocument/diagnostic"] = {
            "kind": "full",
            "items": [_F401],
        }
        session.request_results["textDocument/codeAction"] = []

        await service.get_code_actions(file_path, "import os\n", _WHOLE_FILE)

        context = session.only_request("textDocument/codeAction").params["context"]
        assert context["diagnostics"] == [_F401]


async def test_the_diagnostic_pull_happens_while_the_document_is_open(
    tmp_path: Path,
) -> None:
    """A pull naming a document the server does not hold is answered with an
    empty report, not an error — so it must be sent inside the document's lease,
    between the didOpen and the didClose, or every file looks clean."""
    file_path = tmp_path / "subject.py"

    async with _running_service(
        file_path, "x = 1\n", server_capabilities=_PULL_CAPABLE
    ) as (service, session, file_editor):
        file_editor.get_opened_files = lambda: []  # type: ignore[method-assign]
        session.request_results["textDocument/diagnostic"] = {
            "kind": "full",
            "items": [],
        }

        await service.check_file(file_path)

        assert session.traffic.index("textDocument/didOpen") < session.traffic.index(
            "textDocument/diagnostic"
        )
        assert session.traffic.index("textDocument/diagnostic") < session.traffic.index(
            "textDocument/didClose"
        )


async def test_an_unchanged_report_keeps_the_diagnostics_it_refers_back_to(
    tmp_path: Path,
) -> None:
    """``kind: "unchanged"`` means "same as the result you already have", not
    "no diagnostics". Reading it as the latter would report a file as clean the
    moment a server started answering that way."""
    file_path = tmp_path / "subject.py"

    async with _running_service(
        file_path, "import os\n", server_capabilities=_PULL_CAPABLE
    ) as (service, session, _):
        session.request_results["textDocument/diagnostic"] = {
            "kind": "full",
            "items": [_F401],
            "resultId": "r1",
        }
        assert await service.check_file(file_path) == [_F401]

        session.request_results["textDocument/diagnostic"] = {
            "kind": "unchanged",
            "resultId": "r1",
        }

        assert await service.check_file(file_path) == [_F401]
