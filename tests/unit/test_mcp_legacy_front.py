"""The legacy front owns negotiation, encoding and outbound MCP effects.

The front never talks to the WM directly: every tool call goes through the
core with an explicit service argument, and every outbound message goes
through the session the front was assembled with. A front that answered from
a stale capability would ask a client that cannot answer and leave the run
waiting out the WM's whole deadline.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest

from finecode.mcp_server import core as core_module
from finecode.mcp_server import legacy
from finecode.mcp_server.core import ToolService
from finecode_jsonrpc import JsonRpcServerSession


@pytest.fixture(autouse=True)
def _without_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capability assertions assume no CI; the declaration is env-dependent."""
    monkeypatch.delenv("CI", raising=False)


class _RecordingSession:
    """Stands in for the JSON-RPC session toward the AI client."""

    def __init__(self) -> None:
        self.request_handlers: dict[str, typing.Any] = {}
        self.notification_handlers: dict[str, typing.Any] = {}
        self.requests_sent: list[dict[str, typing.Any]] = []
        self.notifications_sent: list[tuple[str, typing.Any]] = []
        self.request_result: typing.Any = {
            "action": "accept",
            "content": {"choice": "yes"},
        }
        self.request_error: BaseException | None = None
        self.notify_gate: asyncio.Event | None = None
        self.notify_error: BaseException | None = None

    def on_request(self, method: str, handler: typing.Any) -> None:
        self.request_handlers[method] = handler

    def on_notification(self, method: str, handler: typing.Any) -> None:
        self.notification_handlers[method] = handler

    async def send_request(
        self,
        method: str,
        params: dict[str, typing.Any] | None = None,
        timeout: float | None = None,
    ) -> typing.Any:
        self.requests_sent.append(
            {"method": method, "params": params, "timeout": timeout}
        )
        if self.request_error is not None:
            raise self.request_error
        return self.request_result

    async def send_notification(
        self, method: str, params: dict[str, typing.Any] | None = None
    ) -> None:
        if self.notify_gate is not None:
            await self.notify_gate.wait()
        if self.notify_error is not None:
            raise self.notify_error
        self.notifications_sent.append((method, params))


class _RecordingClient:
    """Minimal ApiClient double for the paths the front tests drive."""

    def __init__(self) -> None:
        self.configure_calls: list[dict[str, typing.Any]] = []
        self.connect_calls: list[dict[str, typing.Any]] = []
        self.close_calls = 0
        self.notification_handlers: dict[str, typing.Any] = {}
        self.request_handlers: dict[str, typing.Any] = {}
        self.actions: list[dict[str, typing.Any]] = []
        self.schemas: dict[str, typing.Any] = {}
        self.reload_config_result: typing.Any = [
            {"project": "/probe", "status": "recovered"}
        ]
        self.run_action_calls: list[dict[str, typing.Any]] = []
        self.run_action_gate: asyncio.Event | None = None
        self.run_action_result: typing.Any = None

    def on_notification(self, name: str, handler: typing.Any) -> None:
        self.notification_handlers[name] = handler

    def on_request(self, name: str, handler: typing.Any) -> None:
        self.request_handlers[name] = handler

    def configure_reconnect(self, policy: typing.Any, on_reattach: typing.Any) -> None:
        self.configure_calls.append({"policy": policy, "on_reattach": on_reattach})

    async def connect(
        self,
        host: str,
        port: int,
        client_id: str | None = None,
        capabilities: dict[str, typing.Any] | None = None,
    ) -> None:
        self.connect_calls.append(
            {
                "host": host,
                "port": port,
                "client_id": client_id,
                "capabilities": capabilities,
            }
        )

    async def close(self) -> None:
        self.close_calls += 1

    async def add_dir(
        self, dir_path: typing.Any, start_runners: bool = True
    ) -> dict[str, typing.Any]:
        _ = (dir_path, start_runners)
        return {"projects": []}

    async def list_actions(self, **kwargs: typing.Any) -> typing.Any:
        from types import (
            SimpleNamespace,  # noqa: PLC0415; test-local stand-in mirrors core tests
        )

        return SimpleNamespace(actions=self.actions, unresolved_projects=[])

    async def get_payload_schemas(
        self, _project: str, sources: list[str]
    ) -> dict[str, typing.Any]:
        return {source: self.schemas.get(source) for source in sources}

    async def reload_config(self, **kwargs: typing.Any) -> typing.Any:
        return self.reload_config_result

    async def run_action(
        self,
        action_source: str,
        project: str,
        params: dict[str, typing.Any] | None = None,
        options: dict[str, typing.Any] | None = None,
        *,
        progress_token: str | None = None,
        partial_result_token: str | None = None,
    ) -> typing.Any:
        self.run_action_calls.append(
            {
                "action_source": action_source,
                "project": project,
                "params": params,
                "options": options,
                "progress_token": progress_token,
                "partial_result_token": partial_result_token,
            }
        )
        if self.run_action_gate is not None:
            await self.run_action_gate.wait()
        if self.run_action_result is not None:
            return self.run_action_result
        return {
            "results": {
                project or "/probe": {
                    action_source: {
                        "resultByFormat": {"echo": params or {}},
                        "returnCode": 0,
                    }
                }
            },
            "returnCode": 0,
        }


def _assemble(
    session: _RecordingSession | None = None,
    client: _RecordingClient | None = None,
) -> tuple[legacy.LegacyFront, ToolService, _RecordingSession, _RecordingClient]:
    session = session or _RecordingSession()
    client = client or _RecordingClient()
    front = legacy.LegacyFront(typing.cast(typing.Any, session))
    service = ToolService(
        workdir=pathlib.Path("/probe"),
        port=1,
        client=typing.cast(typing.Any, client),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )
    return front, service, session, client


# ---------------------------------------------------------------------------
# Initialize branches and F1: live capability, connect-time configuration
# ---------------------------------------------------------------------------


async def test_initialize_defaults_without_params() -> None:
    """A client that states nothing gets the newest revision and no questions."""
    front, _, _, _ = _assemble()

    result = await front.handle_initialize(None)

    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": True}}
    assert result["serverInfo"] == {"name": "FineCode", "version": "1.0.0"}
    assert front.connection_config().client_id == "mcp"
    assert front.connection_config().can_answer_choices is False


async def test_empty_initialize_params_retain_current_state() -> None:
    """Empty params are not a reset: the stated identity survives them."""
    front, _, _, _ = _assemble()
    await front.handle_initialize(
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"elicitation": {"choice": True}},
            "clientInfo": {"name": "editor", "version": "1"},
        }
    )

    result = await front.handle_initialize({})

    assert result["protocolVersion"] == "2025-06-18"
    assert front.connection_config().client_id == "mcp-editor"
    assert front.connection_config().can_answer_choices is True


async def test_older_revision_is_echoed_and_clears_elicitation() -> None:
    """An older revision never carries a question, even when one was declared."""
    front, _, _, _ = _assemble()

    result = await front.handle_initialize(
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {"elicitation": True},
            "clientInfo": {"name": "old", "version": "1"},
        }
    )

    assert result["protocolVersion"] == "2024-11-05"
    assert front.connection_config().client_id == "mcp-old"
    assert front.connection_config().can_answer_choices is False


async def test_unsupported_revision_falls_back_to_newest() -> None:
    """An unknown revision is answered with the version actually spoken."""
    front, _, _, _ = _assemble()

    result = await front.handle_initialize(
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {"elicitation": {"choice": True}},
            "clientInfo": {"name": "new", "version": "1"},
        }
    )

    assert result["protocolVersion"] == "2025-06-18"
    assert front.connection_config().can_answer_choices is True


async def test_repeated_initialize_overwrites_identity() -> None:
    """The latest handshake describes the client, not the first one."""
    front, _, _, _ = _assemble()
    await front.handle_initialize({"clientInfo": {"name": "first", "version": "1"}})

    await front.handle_initialize({"clientInfo": {"name": "second", "version": "1"}})

    assert front.current_client_label() == "mcp-second"


async def test_connect_before_initialize_uses_defaults() -> None:
    """Tools used before any handshake connect anonymously and unaskable."""
    _, service, _, client = _assemble()

    await service.ensure_connected()

    assert client.connect_calls[0]["client_id"] == "mcp"
    assert client.connect_calls[0]["capabilities"] is None


async def test_initialize_before_first_connect_sets_identity_and_capability() -> None:
    """A handshake before first use changes the first connect itself."""
    front, service, _, client = _assemble()
    await front.handle_initialize(
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"elicitation": {"choice": True}},
            "clientInfo": {"name": "editor", "version": "1"},
        }
    )

    await service.ensure_connected()

    assert client.connect_calls[0]["client_id"] == "mcp-editor"
    assert client.connect_calls[0]["capabilities"] == {"elicitation": {"choice": True}}


async def test_initialize_after_connect_toggles_questions_without_reconnect_config() -> (
    None
):
    """Later handshakes change question handling at once; the WM keeps its own.

    The reconnect configuration was retained by the client at connect time and
    is never rewritten by a subsequent handshake.
    """
    front, service, _, client = _assemble()
    await service.ensure_connected()
    elicit = client.request_handlers["client/elicit"]

    assert await elicit({"message": "m?", "options": ["a"]}) == {
        "outcome": "unavailable"
    }

    await front.handle_initialize(
        {
            "capabilities": {"elicitation": {"choice": True}},
            "clientInfo": {"name": "e", "version": "1"},
        }
    )
    assert await elicit({"message": "m?", "options": ["yes", "no"]}) == {
        "outcome": "answered",
        "value": "yes",
    }

    await front.handle_initialize(
        {"capabilities": {}, "clientInfo": {"name": "e", "version": "1"}}
    )
    assert await elicit({"message": "m?", "options": ["a"]}) == {
        "outcome": "unavailable"
    }
    assert len(client.connect_calls) == 1
    assert client.connect_calls[0]["capabilities"] is None


async def test_replacing_the_session_moves_later_questions() -> None:
    """The question path reads the current session, never a captured one."""
    front, service, first, client = _assemble()
    await service.ensure_connected()
    elicit = client.request_handlers["client/elicit"]
    second = _RecordingSession()
    second.request_result = {"action": "decline"}

    assert await elicit({"message": "m?", "options": ["a"]}) == {
        "outcome": "unavailable"
    }
    await front.handle_initialize(
        {
            "capabilities": {"elicitation": True},
            "clientInfo": {"name": "e", "version": "1"},
        }
    )
    assert await elicit({"message": "m?", "options": ["yes", "no"]}) == {
        "outcome": "answered",
        "value": "yes",
    }

    front._session = typing.cast(typing.Any, second)
    assert await elicit({"message": "m?", "options": ["a"]}) == {"outcome": "declined"}
    assert len(first.requests_sent) == 1
    assert len(second.requests_sent) == 1


# ---------------------------------------------------------------------------
# F2 across the real core/front wiring
# ---------------------------------------------------------------------------


async def test_recovery_response_waits_for_the_list_change_send() -> None:
    """The client is told its list changed before the recovery answers."""
    front, service, session, client = _assemble()
    client.reload_config_result = [
        {
            "project": "/probe",
            "status": "recovered",
            "actionsAdded": ["new"],
            "actionsRemoved": [],
        }
    ]
    session.notify_gate = asyncio.Event()
    service._tool_name_to_source["old"] = "pkg.Old"
    task = asyncio.create_task(
        front.handle_call_tool(service, {"name": "reload_config", "arguments": {}})
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not task.done()
    assert service._tool_name_to_source == {}
    assert session.notifications_sent == []

    session.notify_gate.set()
    result = await task

    assert result["content"][0]["text"]
    assert ("notifications/tools/list_changed", {}) in session.notifications_sent


async def test_reattach_waits_for_the_list_change_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnected session is re-announced before the re-attach completes."""
    _, service, session, _ = _assemble()
    monkeypatch.setattr(core_module.wm_lifecycle, "running_port", lambda: 2)
    session.notify_gate = asyncio.Event()
    await service.ensure_connected()
    task = asyncio.create_task(service.attach_session(first_connect=False))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not task.done()
    assert service._tool_name_to_source == {}

    session.notify_gate.set()
    await task
    assert ("notifications/tools/list_changed", {}) in session.notifications_sent


async def test_notification_failure_fails_the_recovery_call() -> None:
    """A list-change that cannot be sent fails the recovery the same way."""

    class _Sentinel(Exception):
        pass

    front, service, session, client = _assemble()
    client.reload_config_result = [
        {
            "project": "/probe",
            "status": "recovered",
            "actionsAdded": ["new"],
            "actionsRemoved": [],
        }
    ]
    session.notify_error = _Sentinel("send failed")

    with pytest.raises(_Sentinel, match="send failed"):
        await front.handle_call_tool(
            service, {"name": "reload_config", "arguments": {}}
        )


async def test_notification_failure_fails_reattach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-attach whose announcement fails is not reported as connected."""

    class _Sentinel(Exception):
        pass

    _, service, session, _ = _assemble()
    monkeypatch.setattr(core_module.wm_lifecycle, "running_port", lambda: 2)
    session.notify_error = _Sentinel("send failed")
    with pytest.raises(_Sentinel, match="send failed"):
        await service.attach_session(first_connect=False)


# ---------------------------------------------------------------------------
# Registration, error projection and the telemetry boundary
# ---------------------------------------------------------------------------


async def test_ping_returns_empty() -> None:
    """Ping stays the liveness probe it always was."""
    front, _, _, _ = _assemble()

    assert await front.handle_ping({}) == {}
    assert await front.handle_ping(None) == {}


async def test_register_exposes_the_legacy_methods_on_a_real_session() -> None:
    """The transport serves initialize, ping, tools and the initialized note."""
    session = JsonRpcServerSession()
    front = legacy.LegacyFront(session)
    service = ToolService(
        workdir=pathlib.Path("/probe"),
        port=1,
        client=typing.cast(typing.Any, _RecordingClient()),
        dependencies=front.dependencies(),
        effects=front.effects(),
    )

    front.register(service)

    assert sorted(session._request_handlers.keys()) == [
        "initialize",
        "ping",
        "tools/call",
        "tools/list",
    ]
    assert "notifications/initialized" in session._notification_handlers
    assert "$/cancelRequest" in session._notification_handlers


async def test_missing_params_short_circuits_before_span_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call without params is a front error, not a span and not a connect."""
    entered: list[tuple[str, str]] = []
    front, service, _, client = _assemble()
    monkeypatch.setattr(legacy.telemetry, "mcp_tool_span", _recording_span(entered))

    result = await front.handle_call_tool(service, None)

    assert result == {
        "content": [{"type": "text", "text": "Missing params"}],
        "isError": True,
    }
    assert entered == []
    assert client.connect_calls == []


async def test_valid_call_runs_inside_one_telemetry_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The span covers the awaited core work, from dispatch to result."""
    entered: list[tuple[str, str]] = []
    front, service, _, client = _assemble()
    client.run_action_gate = asyncio.Event()
    monkeypatch.setattr(legacy.telemetry, "mcp_tool_span", _recording_span(entered))
    task = asyncio.create_task(
        front.handle_call_tool(service, {"name": "probe_action", "arguments": {}})
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert entered == [("enter", "probe_action")]
    client.run_action_gate.set()
    result = await task

    assert entered == [("enter", "probe_action"), ("exit", "probe_action")]
    assert "isError" not in result


async def test_core_failure_projects_as_literal_error_text() -> None:
    """A refused built-in stays literal text, never JSON-quoted."""
    front, service, _, _ = _assemble()

    result = await front.handle_call_tool(
        service, {"name": "reload_action", "arguments": {}}
    )

    assert result == {
        "content": [{"type": "text", "text": "Missing 'action' argument"}],
        "isError": True,
    }


def _recording_span(entered: list[tuple[str, str]]) -> typing.Any:
    def _span(name: str) -> typing.Any:
        class _Guard:
            def __enter__(self) -> typing.Self:
                entered.append(("enter", name))
                return self

            def __exit__(self, *args: object) -> bool:
                entered.append(("exit", name))
                return False

        return _Guard()

    return _span


# ---------------------------------------------------------------------------
# Instance ownership
# ---------------------------------------------------------------------------


async def test_two_pairs_keep_separate_identity_and_targets() -> None:
    """Two assembled pairs never share identity, catalog or notifications."""
    front_a, service_a, session_a, client_a = _assemble()
    front_b, service_b, session_b, client_b = _assemble()
    await front_a.handle_initialize(
        {
            "capabilities": {"elicitation": {"choice": True}},
            "clientInfo": {"name": "a", "version": "1"},
        }
    )
    await front_b.handle_initialize(
        {"capabilities": {}, "clientInfo": {"name": "b", "version": "1"}}
    )
    await service_a.ensure_connected()
    await service_b.ensure_connected()

    assert client_a.connect_calls[0]["client_id"] == "mcp-a"
    assert client_b.connect_calls[0]["client_id"] == "mcp-b"
    assert client_a.connect_calls[0]["capabilities"] == {
        "elicitation": {"choice": True}
    }
    assert client_b.connect_calls[0]["capabilities"] is None

    elicit_a = client_a.request_handlers["client/elicit"]
    elicit_b = client_b.request_handlers["client/elicit"]
    assert await elicit_a({"message": "m?", "options": ["yes", "no"]}) == {
        "outcome": "answered",
        "value": "yes",
    }
    assert await elicit_b({"message": "m?", "options": ["x"]}) == {
        "outcome": "unavailable"
    }
    assert len(session_a.requests_sent) == 1
    assert session_b.requests_sent == []

    service_a._tool_name_to_source["only-a"] = "pkg.A"
    assert service_b._tool_name_to_source == {}
