"""Contract tests against the real pyrefly LSP server.

The generated config is the only way to set per-kind severity on the LSP path,
and pyrefly's failure modes there are silent: a config whose includes do not
match the project checks nothing, a project whose excludes contain its source
roots checks nothing, and an invalid config is ignored without an error. These
tests drive the installed pyrefly binary directly, with the production renderer,
so a pyrefly upgrade that breaks the contract turns red instead of silently
dropping the configured kinds.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import typing
from pathlib import Path
from types import TracebackType

import pytest

from fine_python_pyrefly._error_config import (
    PyreflyErrorSeverity,
    dump_config_args,
    render_lsp_config,
)

_DIAGNOSTICS_TIMEOUT_SEC = 15.0
# pyrefly publishes an early set while a file is still resolving imports and a
# corrected set shortly after; a new publish restarts this window, so waiting it
# out returns the corrected set rather than the incomplete early one. Generous
# relative to the observed sub-second gap so a loaded CI host still settles.
_DIAGNOSTICS_QUIET_SEC = 3.0


class _PyreflyLspClient:
    """Minimal stdio JSON-RPC client for the server contract test."""

    def __init__(self, settings: dict[str, typing.Any], project_dir: Path) -> None:
        self._settings = settings
        self._project_dir = project_dir
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self._next_request_id = 0
        self._responses: dict[int, asyncio.Future[dict[str, typing.Any]]] = {}
        self._diagnostics: dict[str, list[dict[str, typing.Any]]] = {}
        self._diagnostics_events: dict[str, asyncio.Event] = {}
        self._publish_counts: dict[str, int] = {}

    async def __aenter__(self) -> typing.Self:
        self._process = await asyncio.create_subprocess_exec(
            _pyrefly_binary(),
            "lsp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        root_uri = self._project_dir.as_uri()
        await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {
                    "workspace": {"workspaceFolders": True, "configuration": True},
                    "textDocument": {
                        "synchronization": {
                            "dynamicRegistration": False,
                            "didSave": True,
                        },
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                },
                "workspaceFolders": [{"uri": root_uri, "name": str(self._project_dir)}],
                "initializationOptions": {"settings": self._settings},
            },
        )
        await self._notify("initialized", {})
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        if self._process is not None and self._process.returncode is None:
            try:
                await self._request("shutdown", None, timeout=5.0)
                await self._notify("exit", {})
                await asyncio.wait_for(self._process.wait(), 5.0)
            except (TimeoutError, ConnectionError):
                self._process.terminate()
                await self._process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()

    async def open_document(self, file_path: Path, content: str) -> str:
        uri = file_path.as_uri()
        self._diagnostics_events.setdefault(uri, asyncio.Event())
        await self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": content,
                }
            },
        )
        return uri

    async def send_configuration(self, settings: dict[str, typing.Any]) -> None:
        # Update before sending: the server pulls ``workspace/configuration``
        # after the notification and must get the new settings, not the old.
        self._settings = settings
        await self._notify("workspace/didChangeConfiguration", {"settings": settings})

    def publish_count(self, uri: str) -> int:
        return self._publish_counts.get(uri, 0)

    async def wait_for_last_diagnostics(
        self,
        uri: str,
        *,
        after: int = 0,
        timeout: float = _DIAGNOSTICS_TIMEOUT_SEC,
        quiet_sec: float = _DIAGNOSTICS_QUIET_SEC,
    ) -> list[dict[str, typing.Any]]:
        """Return the last publish for *uri* once publishes go quiet.

        Waits for a publish beyond *after* first, so a caller that changed the
        server state can ask for a publish the change produced rather than
        immediately returning the previous one. The deadline bounds the whole
        wait; the quiet window decides when the last publish has arrived.
        """
        event = self._diagnostics_events.setdefault(uri, asyncio.Event())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._publish_counts.get(uri, 0) <= after:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"No diagnostics for {uri} within {timeout}s;"
                    f" stderr: {''.join(self._stderr)}"
                )
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                raise AssertionError(
                    f"No diagnostics for {uri} within {timeout}s;"
                    f" stderr: {''.join(self._stderr)}"
                ) from None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._diagnostics.get(uri, [])
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), min(quiet_sec, remaining))
            except TimeoutError:
                return self._diagnostics.get(uri, [])

    async def _read_messages(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        while True:
            try:
                header = await self._process.stdout.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            content_length = 0
            for line in header.decode("ascii").splitlines():
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            body = await self._process.stdout.readexactly(content_length)
            message = json.loads(body)
            if "method" in message:
                if "id" in message:
                    await self._answer_server_request(message)
                elif message["method"] == "textDocument/publishDiagnostics":
                    self._record_diagnostics(message["params"])
                continue
            future = self._responses.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message)

    async def _answer_server_request(self, message: dict[str, typing.Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "workspace/configuration":
            items = params.get("items", [])
            result: typing.Any = (
                [self._settings for _ in items] if items else [self._settings]
            )
        elif method == "workspace/workspaceFolders":
            root_uri = self._project_dir.as_uri()
            result = [{"uri": root_uri, "name": str(self._project_dir)}]
        else:
            result = None
        await self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _record_diagnostics(self, params: dict[str, typing.Any]) -> None:
        uri = params["uri"]
        self._diagnostics[uri] = params["diagnostics"]
        self._publish_counts[uri] = self._publish_counts.get(uri, 0) + 1
        event = self._diagnostics_events.get(uri)
        if event is not None:
            event.set()

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode("utf-8", "replace"))

    async def _request(
        self,
        method: str,
        params: dict[str, typing.Any] | None,
        timeout: float = 30.0,
    ) -> dict[str, typing.Any]:
        self._next_request_id += 1
        request_id = self._next_request_id
        future: asyncio.Future[dict[str, typing.Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._responses[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return await asyncio.wait_for(future, timeout)

    async def _notify(self, method: str, params: dict[str, typing.Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, typing.Any]) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        body = json.dumps(message).encode("utf-8")
        self._process.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        await self._process.stdin.drain()


def _pyrefly_binary() -> str:
    binary = shutil.which("pyrefly", path=str(Path(sys.executable).parent))
    if binary is None:
        raise AssertionError(
            f"pyrefly is not installed in {Path(sys.executable).parent}; the"
            " testing environment declares it as a dependency, so a missing"
            " binary is a failure rather than a skip"
        )
    return binary


class _LspProject(typing.NamedTuple):
    project_dir: Path
    extra_dir: Path
    file_path: Path
    config_path: Path
    content: str


def _make_project(
    tmp_path: Path, errors: dict[str, PyreflyErrorSeverity]
) -> _LspProject:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir()
    (extra_dir / "mypkg_zz.py").write_text("X = 1\n", encoding="utf-8")
    file_path = project_dir / "main.py"
    content = (
        "from mypkg_zz import X\n"
        "\n"
        "\n"
        "def f(a: dict) -> None:\n"
        "    print(a)\n"
        "\n"
        "\n"
        "y: str = X\n"
    )
    file_path.write_text(content, encoding="utf-8")
    # The generated config lives outside the project tree so pyrefly cannot
    # discover it by its own upward walk; the late-push test relies on the
    # server not having it before the notification.
    config_path = tmp_path / "cache" / "pyrefly.toml"
    config_path.parent.mkdir()
    config_path.write_text(render_lsp_config(project_dir, errors), encoding="utf-8")
    return _LspProject(project_dir, extra_dir, file_path, config_path, content)


def _settings(
    project: _LspProject, *, config_path: Path | None
) -> dict[str, typing.Any]:
    pyrefly_settings: dict[str, typing.Any] = {
        "extraPaths": [str(project.extra_dir)],
        "displayTypeErrors": "force-on",
    }
    if config_path is not None:
        pyrefly_settings["configPath"] = str(config_path)
    return {"pythonPath": sys.executable, "pyrefly": pyrefly_settings}


def _codes(diagnostics: list[dict[str, typing.Any]]) -> list[str]:
    return [str(diagnostic.get("code")) for diagnostic in diagnostics]


async def test_config_path_enables_kinds_and_kind_severities(tmp_path: Path) -> None:
    """A config rendered by the handler must turn on the configured kinds, apply
    their severities, and leave ``extraPaths`` resolution working.

    If any of that stops holding, pyrefly silently reports the wrong set — the
    configured kind never appears, or an unrelated severity change goes
    unnoticed — rather than failing the run.
    """
    project = _make_project(
        tmp_path,
        {"implicit-any-type-argument": "warn", "bad-assignment": "warn"},
    )
    settings = _settings(project, config_path=project.config_path)
    async with _PyreflyLspClient(settings, project.project_dir) as client:
        uri = await client.open_document(project.file_path, project.content)
        diagnostics = await client.wait_for_last_diagnostics(uri)

    assert "implicit-any-type-argument" in _codes(diagnostics)
    bad_assignments = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.get("code") == "bad-assignment"
    ]
    assert bad_assignments
    assert all(diagnostic.get("severity") == 2 for diagnostic in bad_assignments)
    # `mypkg_zz` is reachable only through extraPaths; a missing-import here
    # means the generated config turned extraPaths off.
    assert "missing-import" not in _codes(diagnostics)


async def test_config_path_pushed_after_start_takes_effect(tmp_path: Path) -> None:
    """A configPath delivered after the server started must reach it.

    This is the handler-after-hover path: without the late push, the configured
    kind never appears for the rest of the session with no error anywhere.
    """
    project = _make_project(
        tmp_path,
        {"implicit-any-type-argument": "warn", "bad-assignment": "warn"},
    )
    async with _PyreflyLspClient(
        _settings(project, config_path=None), project.project_dir
    ) as client:
        uri = await client.open_document(project.file_path, project.content)
        before = await client.wait_for_last_diagnostics(uri)
        assert "implicit-any-type-argument" not in _codes(before)

        mark = client.publish_count(uri)
        await client.send_configuration(
            _settings(project, config_path=project.config_path)
        )
        after = await client.wait_for_last_diagnostics(uri, after=mark)

    assert "implicit-any-type-argument" in _codes(after)


async def test_invalid_error_kind_makes_dump_config_fail(tmp_path: Path) -> None:
    """A config with an unknown kind must fail ``dump-config``.

    pyrefly's LSP ignores an invalid config file wholesale with no error, so
    this check is what turns a bad generated config into a failing run instead
    of a silent loss of every configured kind.
    """
    project = _make_project(tmp_path, {"bogus-kind": "warn"})
    process = await asyncio.create_subprocess_exec(
        *dump_config_args(Path(_pyrefly_binary()), project.config_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode != 0, (stdout, stderr)


async def test_dump_config_validation_does_not_walk_the_project(
    tmp_path: Path,
) -> None:
    """A project file the walker rejects must not fail config validation.

    A stale setuptools ``build/`` editable install leaves symlinks pointing at
    the machine that built it; project-checking mode aborts on them, while the
    LSP server itself tolerates them. Validation must not be stricter than the
    consumer it protects.
    """
    project = _make_project(tmp_path, {"implicit-any-type-argument": "warn"})
    stale_dir = project.project_dir / "build" / "pkg"
    stale_dir.mkdir(parents=True)
    try:
        os.symlink(str(tmp_path / "gone" / "module.py"), stale_dir / "module.py")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    process = await asyncio.create_subprocess_exec(
        *dump_config_args(Path(_pyrefly_binary()), project.config_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, (stdout, stderr)
