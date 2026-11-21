"""FineCode clients in CI declare no question capability (D13, ADR-0163).

A process with a non-empty ``CI`` never declares elicitation support: every
question on its connection is answered unavailable at once, without reaching
the client's handler. Outside CI a terminal CLI and a capable MCP server
declare exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import typing

import pytest
from finecode_extension_api.interfaces.iuserprompt import ElicitationOutcome
from finecode_extension_runner.impls.user_prompt import UserPrompt
from loguru import logger

from finecode import wm_client
from finecode.cli_app.commands import run_cmd
from finecode.mcp_server import core
from finecode.mcp_server.contracts import (
    ToolDependencies,
    ToolEffects,
    WmConnectionConfig,
)
from finecode.wm_server import context, wm_server
from finecode.wm_server.runner import elicitation_bridge
from finecode_extension_runner import run_context

_RUN_ID = "run-0001"


@pytest.fixture(autouse=True)
def _restore_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


@pytest.mark.parametrize(
    ("ci_value", "allowed"),
    [
        (None, True),
        ("", True),
        ("true", False),
        ("false", False),
        ("0", False),
        ("1", False),
    ],
)
def test_ci_emptiness_decides_the_declaration(
    monkeypatch: pytest.MonkeyPatch, ci_value: str | None, allowed: bool
) -> None:
    """Only an empty or absent ``CI`` declares; even ``CI=false`` mutes it."""
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)

    assert wm_client.question_capability_allowed(client_label="probe") is allowed


def test_a_muted_client_names_itself_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one info line says whose declaration was withheld, and why."""
    monkeypatch.setenv("CI", "true")
    records: list[str] = []
    handle = logger.add(records.append, level="INFO")
    try:
        assert wm_client.question_capability_allowed(client_label="cli") is False
    finally:
        logger.remove(handle)

    assert len(records) == 1
    assert "cli" in records[0]
    assert "CI" in records[0]


def test_a_pipeline_logs_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TTY there is no declaration either way, and no new log."""
    monkeypatch.delenv("CI", raising=False)
    records: list[str] = []
    handle = logger.add(records.append, level="INFO")
    try:
        assert wm_client.question_capability_allowed(client_label="cli") is True
    finally:
        logger.remove(handle)

    assert records == []


class _RecordingCliClient:
    """Stand-in for ``ApiClient`` covering what ``run_actions`` touches."""

    def __init__(self) -> None:
        self.request_handlers: dict[str, typing.Any] = {}
        self.connect_kwargs: dict[str, typing.Any] = {}
        self.closed = False
        self._on_reattach: typing.Any = None

    def on_notification(self, method: str, handler: typing.Any) -> None:
        pass

    def on_request(self, method: str, handler: typing.Any) -> None:
        self.request_handlers[method] = handler

    def configure_reconnect(self, policy: object, **kwargs: object) -> None:
        self._on_reattach = kwargs.get("on_reattach")

    async def subscribe_logs(self, min_level: str) -> None: ...

    async def set_config_overrides(
        self, handler_overrides: object, service_overrides: object
    ) -> None: ...

    async def connect(self, host: str, port: int, **kwargs: object) -> None:
        self.connect_kwargs = kwargs
        on_reattach = getattr(self, "_on_reattach", None)
        if on_reattach is not None:
            await on_reattach(first_connect=True)

    async def add_dir(
        self, dir_path: pathlib.Path, start_runners: bool = False
    ) -> dict:
        return {"projects": []}

    async def list_projects(self) -> list[dict]:
        return []

    async def list_actions(
        self,
        *,
        project: str | None = None,
        names: list[str] | None = None,
        projects: list[str] | None = None,
    ) -> wm_client.ActionListing:
        return wm_client.ActionListing(actions=[], unresolved_projects=[])

    async def get_payload_schemas(
        self,
        project: str,
        action_sources: list[str],
        *,
        start_runners: bool = False,
        run_options: dict | None = None,
    ) -> dict:
        return {}

    async def run_batch(
        self,
        action_sources: list[str],
        projects: list[str] | None = None,
        **kwargs: object,
    ) -> dict:
        return {"results": {}, "returnCode": 0}

    async def close(self) -> None:
        self.closed = True


async def _run_command(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tty: bool,
    ci_value: str | None,
) -> _RecordingCliClient:
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: tty)
    client = _RecordingCliClient()
    monkeypatch.setattr(run_cmd.ApiClient, "__new__", lambda cls: client)
    monkeypatch.setattr(
        run_cmd.wm_lifecycle,
        "ensure_running",
        lambda *a, **k: None,
    )

    async def _fake_wait_ready(*a: object, **k: object) -> int:
        return 1234

    monkeypatch.setattr(run_cmd.wm_lifecycle, "wait_until_ready", _fake_wait_ready)
    await run_cmd.run_actions(
        workdir_path=tmp_path,
        projects_names=None,
        actions=[],
        action_payload={},
        raw_action_payload={},
        concurrently=False,
        handler_config_overrides=None,
        service_config_overrides=None,
        save_results=False,
        map_payload_fields=None,
        own_server=False,
        log_level="INFO",
    )
    return client


@pytest.mark.parametrize(
    ("tty", "ci_value", "declares"),
    [
        (True, None, True),
        (True, "", True),
        (True, "true", False),
        (True, "false", False),
        (True, "0", False),
        (False, None, False),
        (False, "true", False),
    ],
)
async def test_cli_declaration_follows_tty_and_ci(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tty: bool,
    ci_value: str | None,
    declares: bool,
) -> None:
    """The CLI declares only on a TTY outside CI; the handler follows suit."""
    client = await _run_command(tmp_path, monkeypatch, tty=tty, ci_value=ci_value)

    if declares:
        assert client.connect_kwargs["capabilities"] == {
            "elicitation": {"choice": True}
        }
        assert "client/elicit" in client.request_handlers
    else:
        assert client.connect_kwargs["capabilities"] is None
        assert "client/elicit" not in client.request_handlers


class _RecordingMcpClient:
    """Stands in for ``ApiClient`` for the MCP core's connect path."""

    def __init__(self) -> None:
        self.connect_calls: list[dict[str, typing.Any]] = []
        self.request_handlers: dict[str, typing.Any] = {}

    def on_notification(self, name: str, handler: typing.Any) -> None:
        pass

    def on_request(self, name: str, handler: typing.Any) -> None:
        self.request_handlers[name] = handler

    def configure_reconnect(self, policy: typing.Any, on_reattach: typing.Any) -> None:
        pass

    async def connect(
        self,
        host: str,
        port: int,
        client_id: str | None = None,
        capabilities: dict[str, typing.Any] | None = None,
    ) -> None:
        self.connect_calls.append(
            {"host": host, "client_id": client_id, "capabilities": capabilities}
        )

    async def close(self) -> None:
        pass

    async def add_dir(
        self, dir_path: typing.Any, start_runners: bool = True
    ) -> dict[str, typing.Any]:
        return {"projects": []}


def _mcp_service(
    client: _RecordingMcpClient, *, can_answer: bool, client_id: str
) -> core.ToolService:
    async def _noop(*args: typing.Any, **kwargs: typing.Any) -> None:
        return None

    async def _question(question: typing.Any) -> typing.Any:
        raise AssertionError("no question should be asked in this test")

    return core.ToolService(
        workdir=pathlib.Path("/probe"),
        port=1,
        client=typing.cast(typing.Any, client),
        dependencies=ToolDependencies(
            connection_config=lambda: WmConnectionConfig(
                client_id=client_id, can_answer_choices=can_answer
            ),
            current_client_label=lambda: client_id,
        ),
        effects=ToolEffects(
            partial_result=_noop,
            progress=_noop,
            question=_question,
            catalog_changed=_noop,
        ),
    )


@pytest.mark.parametrize(
    ("ci_value", "declares"),
    [(None, True), ("", True), ("true", False)],
)
async def test_mcp_declaration_follows_ci(
    monkeypatch: pytest.MonkeyPatch, ci_value: str | None, declares: bool
) -> None:
    """A capable MCP server declares outside CI and stays silent inside it."""
    if ci_value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci_value)
    client = _RecordingMcpClient()
    service = _mcp_service(client, can_answer=True, client_id="mcp-test")

    await service.ensure_connected()

    if declares:
        assert client.connect_calls[0]["capabilities"] == {
            "elicitation": {"choice": True}
        }
    else:
        assert client.connect_calls[0]["capabilities"] is None


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    wm_server._pending_client_requests.clear()
    wm_server._pending_client_request_owners.clear()
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    elicitation_bridge.reset_origins()


@pytest.fixture
async def _wm_port():
    """A real WM dispatch loop, minus everything an action would need."""
    wm_server._keep_alive = True
    ws_context = context.WorkspaceContext([])
    server = await asyncio.start_server(
        lambda r, w: wm_server._handle_client(r, w, ws_context), "127.0.0.1", 0
    )
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _ask() -> typing.Any:
    """The ER->WM hop, stubbed with the callback body ``runner_manager`` registers."""

    async def _send_request_to_wm(method: str, params: dict) -> dict:
        installed = elicitation_bridge.handlers()
        assert installed is not None
        return await installed.elicit(
            message=params["message"],
            options=params["options"],
            default=params["default"],
            timeout_sec=params["timeoutSec"],
            run_writer_key=elicitation_bridge.originating_client_for_run(
                params["runId"]
            ),
        )

    return UserPrompt(_send_request_to_wm)


@contextlib.contextmanager
def _a_run_from(connection: object):
    with elicitation_bridge.bind_run(
        _RUN_ID, elicitation_bridge.RunDispatchOrigin(connection=connection)
    ):
        yield


def _server_writer() -> object:
    return next(iter(wm_server._connected_clients))


async def test_ci_client_question_is_unavailable_and_stays_so_after_reconnect(
    _wm_port: int,
) -> None:
    """Nothing declared means no round trip; reconnecting re-declares nothing."""
    first = wm_client.ApiClient()
    first_asked: list[dict] = []

    async def _first_handler(params: dict | None) -> dict:
        first_asked.append(params or {})
        return {"outcome": "answered", "value": "yes"}

    first.on_request("client/elicit", _first_handler)
    await first.connect("127.0.0.1", _wm_port, capabilities=None)
    try:
        await asyncio.sleep(0.05)
        with _a_run_from(_server_writer()), run_context.run(_RUN_ID):
            result = await asyncio.wait_for(
                _ask().ask_choice("Proceed?", ["yes", "no"]), timeout=1.0
            )
        assert result.outcome is ElicitationOutcome.UNAVAILABLE
        assert first_asked == []
    finally:
        await first.close()

    second = wm_client.ApiClient()
    second_asked: list[dict] = []

    async def _second_handler(params: dict | None) -> dict:
        second_asked.append(params or {})
        return {"outcome": "answered", "value": "yes"}

    second.on_request("client/elicit", _second_handler)
    await second.connect("127.0.0.1", _wm_port, capabilities=None)
    try:
        await asyncio.sleep(0.05)
        with _a_run_from(_server_writer()), run_context.run(_RUN_ID):
            again = await asyncio.wait_for(
                _ask().ask_choice("Proceed?", ["yes", "no"]), timeout=1.0
            )
        assert again.outcome is ElicitationOutcome.UNAVAILABLE
        assert second_asked == []
    finally:
        await second.close()


async def test_a_question_pending_at_disconnect_is_not_resent(_wm_port: int) -> None:
    """ADR-0074: a question on screen when the client goes resolves unavailable."""
    client = wm_client.ApiClient()
    hanging = asyncio.Event()

    async def _never_answers(_params: dict | None) -> dict:
        await hanging.wait()
        return {"outcome": "declined"}

    client.on_request("client/elicit", _never_answers)
    await client.connect(
        "127.0.0.1", _wm_port, capabilities={"elicitation": {"choice": True}}
    )
    await asyncio.sleep(0.05)

    with _a_run_from(_server_writer()), run_context.run(_RUN_ID):
        asking = asyncio.create_task(_ask().ask_choice("Proceed?", ["yes", "no"]))
        await asyncio.sleep(0.05)
        await client.close()

        result = await asyncio.wait_for(asking, timeout=2.0)

    assert result.outcome is ElicitationOutcome.UNAVAILABLE
