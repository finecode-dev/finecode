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
        if self.raise_on_send_request is not None:
            raise self.raise_on_send_request
        return None

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
    def __init__(self, content: str) -> None:
        self._content = content

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: Path, block: bool = False
    ) -> AsyncIterator[ifileeditor.FileInfo]:
        yield ifileeditor.FileInfo(content=self._content, version="1")

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(self) -> AsyncIterator[Any]:
        async def _never_yields() -> AsyncIterator[Any]:
            import asyncio

            await asyncio.Event().wait()
            yield  # pragma: no cover - unreachable, keeps this an async generator

        yield _never_yields()


class _FakeFileEditor:
    """Reports the subject file as open in the IDE, so the LSP session for it
    stays open across calls instead of being closed after every request —
    matching the common case where a hover fires while the user has the file
    open for editing.
    """

    def __init__(self, file_path: Path, content: str) -> None:
        self.file_path = file_path
        self.content = content

    @contextlib.asynccontextmanager
    async def session(self, author: Any) -> AsyncIterator[_FakeFileEditorSession]:
        yield _FakeFileEditorSession(self.content)

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
    file_path: Path, content: str
) -> AsyncIterator[tuple[LspService, _FakeLspSession, _FakeFileEditor]]:
    session = _FakeLspSession()
    file_editor = _FakeFileEditor(file_path, content)
    service = LspService(
        lsp_client=_FakeLspClient(session),
        file_editor=file_editor,  # type: ignore[arg-type]
        logger=_NullLogger(),  # type: ignore[arg-type]
        cmd="fake-lsp-server",
        language_id="python",
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
