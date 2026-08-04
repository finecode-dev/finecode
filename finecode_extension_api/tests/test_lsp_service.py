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


class _FakeLspSession:
    """Records notifications instead of talking to a real language server."""

    def __init__(self) -> None:
        self.notifications: list[_SentNotification] = []
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
        try:
            if self.raise_on_send_request is not None:
                raise self.raise_on_send_request
            if self.release_request is not None:
                await self.release_request.wait()
            return None
        finally:
            self.in_flight -= 1

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if self.release_send is not None:
            await self.release_send.wait()
        self.notifications.append(_SentNotification(method, params))

    def on_notification(self, method: str, handler: Any) -> None:
        pass

    def on_request(self, method: str, handler: Any) -> None:
        pass

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return {}

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
    file_path: Path, content: str, max_concurrent_requests: int | None = None
) -> AsyncIterator[tuple[LspService, _FakeLspSession, _FakeFileEditor]]:
    session = _FakeLspSession()
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
