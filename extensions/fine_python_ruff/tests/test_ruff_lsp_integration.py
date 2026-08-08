"""Document-lifecycle tests against a real `ruff server` process.

Everything else in this suite stubs the language server, which means it can only
check that FineCode sends what the test author believed it should. The bugs these
cover were all of the other kind: FineCode sent something defensible and the
server answered nothing. Only a real server settles that — a document ruff does
not hold is not an error, it is an empty result, and a fix ruff cannot match to a
diagnostic is not an error either.

The client here is a minimal stdio transport rather than the production one:
these exercise the lifecycle in `LspService` and the settings and capabilities in
`RuffLspService`, and a real JSON-RPC implementation adds a dependency without
adding coverage of either.

Marked `integration` — they spawn a process and wait on real analysis.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import sys
import time
import typing
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor

from fine_python_ruff.ruff_lsp_service import RuffLspService

pytestmark = pytest.mark.integration

_RUFF_BIN = Path(sys.executable).parent / "ruff"

_SUBJECT = 'import os\n\n\ndef greet():\n    message = f"hello"\n    return message\n'
"""Two diagnostics with fixes: F401 on line 0, F541 on line 4."""

_F541_RANGE = {
    "start": {"line": 4, "character": 14},
    "end": {"line": 4, "character": 22},
}

_WHOLE_FILE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 5, "character": 2**31 - 1},
}

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)


# ----------------------------------------------------------------------
# Minimal LSP transport over stdio
# ----------------------------------------------------------------------


class _StdioLspSession:
    """Speaks LSP to a subprocess: enough of ILspSession for LspService."""

    def __init__(self, cmd: str, root_uri: str, **kwargs: Any) -> None:
        self._cmd = cmd
        self._root_uri = root_uri
        self._client_capabilities = kwargs.get("client_capabilities") or {}
        self._initialization_options = kwargs.get("initialization_options")
        self._workspace_folders = kwargs.get("workspace_folders") or []
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._notification_handlers: dict[str, Any] = {}
        self._request_handlers: dict[str, Any] = {}
        self._next_id = 0
        self._server_capabilities: dict[str, Any] = {}

    async def __aenter__(self) -> Self:
        self._process = await asyncio.create_subprocess_exec(
            *shlex.split(self._cmd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read_loop())
        result = await self.send_request(
            "initialize",
            {
                "processId": None,
                "rootUri": self._root_uri,
                "workspaceFolders": self._workspace_folders,
                "capabilities": self._client_capabilities,
                "initializationOptions": self._initialization_options,
            },
        )
        self._server_capabilities = (result or {}).get("capabilities", {})
        await self.send_notification("initialized", {})
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=5)

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    @property
    def server_info(self) -> dict[str, Any] | None:
        return None

    def on_notification(self, method: str, handler: Any) -> None:
        self._notification_handlers[method] = handler

    def on_request(self, method: str, handler: Any) -> None:
        self._request_handlers[method] = handler

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = 30.0,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return await asyncio.wait_for(future, timeout=timeout)

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        body = json.dumps(payload).encode()
        self._process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        while True:
            length = 0
            while True:
                line = await stdout.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            message = json.loads(await stdout.readexactly(length))
            await self._dispatch(message)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is not None and "id" in message:
            handler = self._request_handlers.get(method)
            # Unhandled server requests are answered with null rather than an
            # error: a server that asks before LspService has registered its
            # handlers (ruff asks for configuration during startup) should not
            # be given a failure it might act on.
            result = await handler(message.get("params")) if handler else None
            self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})
        elif method is not None:
            handler = self._notification_handlers.get(method)
            if handler is not None:
                await handler(message.get("params"))
        else:
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message.get("result"))


class _StdioLspClient:
    def session(self, **kwargs: Any) -> _StdioLspSession:
        return _StdioLspSession(**kwargs)


# ----------------------------------------------------------------------
# A file editor with nothing open — the headless case
# ----------------------------------------------------------------------


class _FileEditorSession:
    def __init__(self, events: asyncio.Queue[ifileeditor.FileEvent]) -> None:
        self._events = events

    @contextlib.asynccontextmanager
    async def read_file(
        self, file_path: Path, block: bool = False
    ) -> AsyncIterator[ifileeditor.FileInfo]:
        content = await asyncio.to_thread(file_path.read_text)
        yield ifileeditor.FileInfo(content=content, version=str(hash(content)))

    @contextlib.asynccontextmanager
    async def subscribe_to_all_events(self) -> AsyncIterator[Any]:
        async def _drain() -> AsyncIterator[Any]:
            while True:
                yield await self._events.get()

        yield _drain()


class _HeadlessFileEditor:
    """No file is ever open, as for every caller without an IDE behind it.

    This is the condition under which documents are opened on demand and closed
    again, and therefore the only one in which any of this is exercised.
    """

    def __init__(self) -> None:
        self.events: asyncio.Queue[ifileeditor.FileEvent] = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def session(self, author: Any) -> AsyncIterator[_FileEditorSession]:
        yield _FileEditorSession(self.events)

    def get_opened_files(self) -> list[Path]:
        return []


class _NullLogger:
    def exception(self, exception: Exception) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...


@pytest.fixture
def subject(tmp_path: Path) -> Path:
    """A file with two fixable diagnostics, in a project ruff will analyze."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "subject"\nversion = "0"\nrequires-python = ">=3.11"\n'
        '\n[tool.ruff.lint]\nselect = ["E", "F"]\n'
    )
    path = tmp_path / "subject.py"
    path.write_text(_SUBJECT)
    return path


@pytest.fixture
async def service(subject: Path) -> AsyncIterator[RuffLspService]:
    if not _RUFF_BIN.exists():
        pytest.skip(f"no ruff binary at {_RUFF_BIN}")

    lsp_service = RuffLspService(
        lsp_client=typing.cast(typing.Any, _StdioLspClient()),
        file_editor=typing.cast(typing.Any, _HeadlessFileEditor()),
        logger=typing.cast(typing.Any, _NullLogger()),
    )
    await lsp_service.ensure_started(subject.parent.as_uri(), _META)
    try:
        yield lsp_service
    finally:
        await lsp_service._lsp_service._async_dispose()


def _titles(actions: list[dict[str, Any]] | None) -> list[str]:
    return [action.get("title", "") for action in actions or []]


def _quickfix_titles(actions: list[dict[str, Any]] | None) -> list[str]:
    return [a.get("title", "") for a in actions or [] if a.get("kind") == "quickfix"]


async def test_a_fix_for_one_diagnostic_comes_back_with_its_edit(
    service: RuffLspService, subject: Path
) -> None:
    """Asking for fixes must produce the fix for a specific diagnostic, with its edit.

    This is the whole purpose of the feature: an agent or an IDE quick-fix menu
    needs "correct this one problem", not "reformat everything in the file".
    A blanket fix-all action is not a substitute — it rewrites regions the caller
    never asked about — and a fix with no edit is worse than none, because it is
    offered, applied, and changes nothing.
    """
    actions = await service.get_code_actions(subject, _SUBJECT, _WHOLE_FILE)

    matching = [
        a
        for a in actions or []
        if a.get("kind") == "quickfix" and "extraneous `f` prefix" in a.get("title", "")
    ]
    assert matching, f"no F541 quickfix among {_titles(actions)}"

    changes = (matching[0].get("edit") or {}).get("changes") or {}
    assert changes.get(subject.as_uri()), "the fix carries no edit for the file"


async def test_fixes_are_returned_again_for_a_file_already_asked_about(
    service: RuffLspService, subject: Path
) -> None:
    """Asking twice about an unchanged file must answer the same both times.

    A document opened to answer a request is closed again afterwards. If the
    second request assumes the server still holds it, that request reaches
    nothing — and every caller without an open editor gets one working answer per
    file per runner, then empty results forever, for as long as the file is
    unchanged.
    """
    first = await service.get_code_actions(subject, _SUBJECT, _WHOLE_FILE)
    second = await service.get_code_actions(subject, _SUBJECT, _WHOLE_FILE)

    assert _quickfix_titles(second) == _quickfix_titles(first)
    assert _quickfix_titles(second), "no quickfixes at all on the second request"


async def test_fixes_are_returned_after_the_same_file_was_linted(
    service: RuffLspService, subject: Path
) -> None:
    """Linting a file first must not stop it from yielding fixes afterwards.

    Lint and fixes share one server per runner, and lint is what runs first in
    practice — an IDE showing diagnostics, or an agent reading them before asking
    how to fix them. If the check leaves the document in a state the fix request
    cannot use, the feature is broken for exactly the sequence everyone performs.
    """
    diagnostics = await service.check_file(subject)
    assert {d.code for d in diagnostics} >= {"F401", "F541"}

    actions = await service.get_code_actions(subject, _SUBJECT, _WHOLE_FILE)

    assert _quickfix_titles(actions), f"only {_titles(actions)} after a check"


async def test_formatting_still_changes_a_file_that_was_checked_first(
    service: RuffLspService, subject: Path
) -> None:
    """A formatter must keep formatting after the file has been linted.

    Same shared server and same ordering as linting before fixes, but the failure
    is quieter: formatting returns no edits, the caller writes the content back
    unchanged, and the run reports success. Nothing distinguishes that from a
    file already correctly formatted, so it does not surface as a bug — it
    surfaces as a formatter that people slowly stop trusting.
    """
    unformatted = "x   =    1\n"
    messy = subject.parent / "messy.py"
    messy.write_text(unformatted)

    await service.check_file(messy)
    formatted = await service.format_file(messy, unformatted)

    assert formatted == "x = 1\n"


async def test_narrowing_to_one_code_drops_the_other_diagnostics_fixes(
    service: RuffLspService, subject: Path
) -> None:
    """Asking about one diagnostic must not return fixes for unrelated ones.

    A caller fixing one problem needs the answer to be about that problem. Fixes
    for everything else in the file are not merely noise: applied blindly by an
    agent, they change code the user never pointed at.
    """
    actions = await service.get_code_actions(
        subject, _SUBJECT, _WHOLE_FILE, diagnostic_codes=["F541"]
    )

    quickfixes = _quickfix_titles(actions)
    assert quickfixes, "narrowing removed every fix"
    assert all("F541" in title for title in quickfixes), quickfixes


async def test_a_cursor_inside_a_diagnostic_is_offered_its_fix(
    service: RuffLspService, subject: Path
) -> None:
    """A request at a caret must offer the fix for the diagnostic it sits in.

    This is what an editor sends when the user opens the quick-fix menu: an empty
    range at the cursor. It is the single most common way the feature is invoked,
    and an empty range is the case naive range comparison gets wrong.
    """
    caret = {"start": _F541_RANGE["start"], "end": _F541_RANGE["start"]}

    actions = await service.get_code_actions(subject, _SUBJECT, caret)

    quickfixes = _quickfix_titles(actions)
    assert any("extraneous `f` prefix" in title for title in quickfixes), quickfixes
    assert not any("`os`" in title for title in quickfixes), (
        f"offered a fix for a diagnostic elsewhere in the file: {quickfixes}"
    )


async def test_ruff_negotiates_pull_diagnostics(
    service: RuffLspService, subject: Path
) -> None:
    """The client capability and ruff's answer must actually meet.

    Everything below depends on which path this lands on, and both paths return
    diagnostics — so a capability that stopped being declared, or a ruff that
    stopped advertising `diagnosticProvider`, would not fail any other test
    here. It would silently restore the push path, and with it the per-clean-file
    settle wait and the close/reopen race the settle wait exists to mask.
    """
    inner = service._lsp_service

    assert inner._supports_pull_diagnostics, (
        "ruff did not advertise diagnosticProvider; the service fell back to"
        f" waiting for pushed diagnostics. capabilities={inner._server_capabilities}"
    )


async def test_a_clean_file_is_answered_without_waiting_it_out(
    service: RuffLspService, tmp_path: Path
) -> None:
    """A file with nothing wrong must not cost a settle wait.

    On the push path an empty result and a not-yet-analyzed acknowledgement are
    the same message, so a clean file was held for the full settle time — once
    per file, sequentially, over every source file `apply_lint_fixes` visits.
    Pulling asks a question and gets an answer, so clean costs the same as dirty.
    """
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n")

    started = time.monotonic()
    diagnostics = await service.check_file(clean)
    elapsed = time.monotonic() - started

    assert diagnostics == []
    assert elapsed < 0.5, f"a clean file took {elapsed:.2f}s; settle wait is back"
