"""The MCP tool core owns WM lifecycle, catalog, dispatch and merging.

The core never touches the stdio protocol: it receives neutral dependencies and
effects, and the front injects its bound methods for those. A core that mixed
up whose connection asks a question would leave a run waiting out the WM's
whole deadline; a core that forgot to clear its action mapping would keep
offering tools the WM no longer has.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest

from finecode.mcp_server import catalog, core
from finecode.mcp_server.contracts import (
    Question,
    QuestionOutcome,
    QuestionResult,
    ToolData,
    ToolDependencies,
    ToolEffects,
    ToolFailure,
    WmConnectionConfig,
)


class _RecordingClient:
    """Stands in for ApiClient, recording every call the core makes."""

    def __init__(self) -> None:
        self.notification_handlers: dict[str, typing.Any] = {}
        self.request_handlers: dict[str, typing.Any] = {}
        self.configure_calls: list[dict[str, typing.Any]] = []
        self.connect_calls: list[dict[str, typing.Any]] = []
        self.connect_error: BaseException | None = None
        self.close_calls = 0
        self.add_dir_calls: list[dict[str, typing.Any]] = []
        self.list_actions_calls: list[dict[str, typing.Any]] = []
        self.actions: list[dict[str, typing.Any]] = []
        self.unresolved: list[dict[str, typing.Any]] = []
        self.list_actions_error: Exception | None = None
        self.schemas: dict[str, typing.Any] = {}
        self.schemas_error: Exception | None = None
        self.projects_result: typing.Any = [{"name": "p", "path": "/probe"}]
        self.runners_result: typing.Any = [{"name": "r", "status": "running"}]
        self.resource_result: typing.Any = {"snapshot": "stub"}
        self.raw_config_result: typing.Any = {"tool": {}}
        self.reload_action_result: typing.Any = {"reloaded": True}
        self.restart_runner_result: typing.Any = {"restarted": []}
        self.reload_config_result: typing.Any = [
            {"project": "/probe", "status": "recovered"}
        ]
        self.info_result: typing.Any = {"pid": 7, "clients": []}
        self.run_action_calls: list[dict[str, typing.Any]] = []
        self.run_action_result: typing.Any = None
        self.run_action_error: BaseException | None = None
        self.run_action_gate: asyncio.Event | None = None
        self.answer_elicitation = True
        self.elicit_requests: list[dict[str, typing.Any]] = []
        self.elicit_reply: dict[str, typing.Any] = {"outcome": "answered"}

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
        if self.connect_error is not None:
            raise self.connect_error

    async def close(self) -> None:
        self.close_calls += 1

    async def add_dir(
        self, dir_path: typing.Any, start_runners: bool = True
    ) -> dict[str, typing.Any]:
        self.add_dir_calls.append({"dir": dir_path, "start_runners": start_runners})
        return {"projects": []}

    async def list_actions(self, **kwargs: typing.Any) -> typing.Any:
        from types import SimpleNamespace

        self.list_actions_calls.append(kwargs)
        if self.list_actions_error is not None:
            raise self.list_actions_error
        return SimpleNamespace(
            actions=self.actions, unresolved_projects=self.unresolved
        )

    async def get_payload_schemas(
        self, _project: str, sources: list[str]
    ) -> dict[str, typing.Any]:
        if self.schemas_error is not None:
            raise self.schemas_error
        return {source: self.schemas.get(source) for source in sources}

    async def list_projects(self) -> typing.Any:
        return self.projects_result

    async def list_runners(self) -> typing.Any:
        return self.runners_result

    async def get_resource_usage(self, include_processes: bool = False) -> typing.Any:
        assert isinstance(include_processes, bool)
        return {"snapshot": "stub", "includeProcesses": include_processes}

    async def get_project_raw_config(self, _project: str) -> typing.Any:
        return self.raw_config_result

    async def reload_action(
        self, action_source: str, project: str | None = None
    ) -> typing.Any:
        return {
            "action_source": action_source,
            "project": project,
            **_as_dict(self.reload_action_result),
        }

    async def restart_runner(self, **kwargs: typing.Any) -> typing.Any:
        return {"kwargs": kwargs, **_as_dict(self.restart_runner_result)}

    async def reload_config(self, **kwargs: typing.Any) -> typing.Any:
        return self.reload_config_result

    async def get_info(self) -> typing.Any:
        return self.info_result

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
        if self.run_action_error is not None:
            raise self.run_action_error
        if self.run_action_gate is not None:
            await self.run_action_gate.wait()
        if self.answer_elicitation and "client/elicit" in self.request_handlers:
            reply = await self.request_handlers["client/elicit"](
                {"message": "Continue?", "options": ["yes", "no"], "timeoutSec": 1}
            )
            self.elicit_requests.append(reply)
        if (
            partial_result_token is not None
            and "actions/partialResult" in self.notification_handlers
        ):
            await self.notification_handlers["actions/partialResult"](
                {"token": partial_result_token, "value": {"partial": 1}}
            )
            await asyncio.sleep(0)
        if (
            progress_token is not None
            and "actions/progress" in self.notification_handlers
        ):
            await self.notification_handlers["actions/progress"](
                {
                    "token": progress_token,
                    "value": {
                        "type": "progress",
                        "message": "working",
                        "percentage": 50,
                    },
                }
            )
            await asyncio.sleep(0)
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


def _as_dict(value: typing.Any) -> dict[str, typing.Any]:
    return value if isinstance(value, dict) else {}


class _Effects:
    """Records the outbound effects the core asks the front for."""

    def __init__(self) -> None:
        self.partials: list[typing.Any] = []
        self.progresses: list[typing.Any] = []
        self.questions: list[Question] = []
        self.question_result = QuestionResult(QuestionOutcome.ANSWERED, "yes")
        self.question_error: BaseException | None = None
        self.catalog_changed_calls = 0
        self.catalog_changed_gate: asyncio.Event | None = None
        self.catalog_changed_error: BaseException | None = None

    async def partial_result(self, value: typing.Any) -> None:
        self.partials.append(value)

    async def progress(self, value: typing.Any) -> None:
        self.progresses.append(value)

    async def question(self, question: Question) -> QuestionResult:
        self.questions.append(question)
        if self.question_error is not None:
            raise self.question_error
        return self.question_result

    async def catalog_changed(self) -> None:
        self.catalog_changed_calls += 1
        if self.catalog_changed_gate is not None:
            await self.catalog_changed_gate.wait()
        if self.catalog_changed_error is not None:
            raise self.catalog_changed_error

    def tool_effects(self) -> ToolEffects:
        return ToolEffects(
            partial_result=self.partial_result,
            progress=self.progress,
            question=self.question,
            catalog_changed=self.catalog_changed,
        )


def _make_service(
    client: _RecordingClient | None = None,
    effects: _Effects | None = None,
    *,
    can_answer: bool = True,
    client_id: str = "mcp-test",
    label: str = "mcp-test",
    config_calls: list[int] | None = None,
    label_calls: list[str] | None = None,
    port: int = 1,
) -> tuple[core.ToolService, _RecordingClient, _Effects]:
    client = client or _RecordingClient()
    recorder = effects or _Effects()

    def connection_config() -> WmConnectionConfig:
        if config_calls is not None:
            config_calls.append(1)
        return WmConnectionConfig(client_id=client_id, can_answer_choices=can_answer)

    def current_client_label() -> str:
        if label_calls is not None:
            label_calls.append(label)
        return label

    service = core.ToolService(
        workdir=pathlib.Path("/probe"),
        port=port,
        client=typing.cast(typing.Any, client),
        dependencies=ToolDependencies(
            connection_config=connection_config,
            current_client_label=current_client_label,
        ),
        effects=recorder.tool_effects(),
    )
    return service, client, recorder


def _action(
    source: str = "probe.Action", name: str = "probe_action", project: str = "/probe"
) -> dict[str, typing.Any]:
    return {"source": source, "name": name, "project": project}


# ---------------------------------------------------------------------------
# Substep 1: bootstrap, attach and connect
# ---------------------------------------------------------------------------


async def test_connection_config_is_read_lazily_after_the_connected_guard() -> None:
    """The WM sees the identity the front holds at first use, not at build.

    A question answered before first use must change the first connect: reading
    the config at construction would freeze the no-elicitation default.
    """
    config_calls: list[int] = []
    service, client, _ = _make_service(config_calls=config_calls)
    assert config_calls == []

    await service.ensure_connected()
    await service.ensure_connected()

    assert config_calls == [1]
    assert len(client.connect_calls) == 1
    assert client.connect_calls[0]["client_id"] == "mcp-test"
    assert client.connect_calls[0]["capabilities"] == {"elicitation": {"choice": True}}


async def test_no_capability_is_declared_when_the_client_cannot_answer() -> None:
    """Without a declared client capability the WM is never offered a question.

    Declaring one the client lacks would turn every ask into a wait for the
    WM's whole deadline.
    """
    service, client, _ = _make_service(can_answer=False)
    await service.ensure_connected()

    assert client.connect_calls[0]["capabilities"] is None


async def test_failed_connect_closes_and_reraises_without_marking_connected() -> None:
    """A connect that died in attach leaves no socket behind and stays offline.

    Otherwise every retried request would open one more connection and leak it.
    """
    service, client, _ = _make_service()
    client.connect_error = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await service.ensure_connected()

    assert client.close_calls == 1
    assert service.is_connected is False
    with pytest.raises(RuntimeError, match="boom"):
        await service.ensure_connected()
    assert len(client.connect_calls) == 2


async def test_first_attach_adds_the_dir_without_invalidation() -> None:
    """First contact tells the WM the workspace; nothing changed yet to report."""
    service, client, recorder = _make_service()
    await service.ensure_connected()
    service._tool_name_to_source["stale"] = "pkg.Stale"

    await service.attach_session(first_connect=True)

    assert client.add_dir_calls == [
        {"dir": pathlib.Path("/probe"), "start_runners": False}
    ]
    assert recorder.catalog_changed_calls == 0
    assert service._tool_name_to_source == {"stale": "pkg.Stale"}


async def test_reattach_refreshes_the_port_and_invalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted WM listens on a new port and may expose new actions.

    A client that kept the old port reconnects to nothing, and one that kept
    its cached list keeps offering tools the WM no longer has.
    """
    monkeypatch.setattr(core.wm_lifecycle, "running_port", lambda: 2)
    service, client, recorder = _make_service(port=1)
    await service.ensure_connected()
    service._tool_name_to_source["stale"] = "pkg.Stale"

    await service.attach_session(first_connect=False)

    assert service._port == 2
    assert len(client.add_dir_calls) == 1
    assert service._tool_name_to_source == {}
    assert recorder.catalog_changed_calls == 1


async def test_two_services_keep_independent_state() -> None:
    """One session reconnecting must not clear another session's tool list."""
    first, _, _ = _make_service(port=1)
    second, _, _ = _make_service(port=2)
    await first.ensure_connected()
    await second.ensure_connected()
    first._tool_name_to_source["a"] = "pkg.A"

    assert second._tool_name_to_source == {}
    assert first._port == 1 and second._port == 2


def test_prepare_wm_with_shared_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a port file the shared server is reused once it is ready."""
    seen: dict[str, typing.Any] = {}

    def _ensure(workdir: pathlib.Path) -> None:
        seen.setdefault("dir", workdir)

    monkeypatch.setattr(core.wm_lifecycle, "ensure_running", _ensure)

    async def _ready() -> int:
        return 9

    monkeypatch.setattr(core.wm_lifecycle, "wait_until_ready", _ready)

    assert core.prepare_wm(pathlib.Path("/probe"), None) == 9
    assert seen["dir"] == pathlib.Path("/probe")


def test_prepare_wm_with_dedicated_port_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """With a port file a dedicated server is started for one client."""
    seen: dict[str, typing.Any] = {}
    port_file = tmp_path / "wm.port"

    def _start(
        workdir: pathlib.Path, port_file: pathlib.Path | None = None
    ) -> pathlib.Path:
        seen["started"] = (workdir, port_file)
        return port_file or tmp_path / "other.port"

    async def _ready(port_file: pathlib.Path) -> int:
        seen["waited"] = port_file
        return 7

    monkeypatch.setattr(core.wm_lifecycle, "start_own_server", _start)
    monkeypatch.setattr(core.wm_lifecycle, "wait_until_ready_from_file", _ready)

    assert core.prepare_wm(pathlib.Path("/probe"), port_file) == 7
    assert seen["started"] == (pathlib.Path("/probe"), port_file)
    assert seen["waited"] == port_file


# ---------------------------------------------------------------------------
# Substep 2: catalog listing and built-in dispatch
# ---------------------------------------------------------------------------


async def test_list_tools_returns_shared_builtins_plus_live_actions() -> None:
    """The tool list is the frozen builtins plus whatever the WM exposes now."""
    service, client, _ = _make_service()
    client.actions = [_action()]
    client.schemas = {
        "probe.Action": {
            "description": "Probe action",
            "properties": {"value": {"type": "string"}},
            "required": [],
        }
    }

    tools = await service.list_tools()

    assert [t["name"] for t in tools[:10]] == [t["name"] for t in catalog.BUILTIN_TOOLS]
    probe = next(t for t in tools if t["name"] == "probe_action")
    assert probe["description"] == "Probe action"
    assert probe["inputSchema"]["properties"]["value"] == {"type": "string"}
    assert "project" in probe["inputSchema"]["properties"]
    assert service._tool_name_to_source == {"probe_action": "probe.Action"}


async def test_list_tools_degrades_when_listing_fails() -> None:
    """A WM that cannot list still offers the builtins, not an error."""
    service, client, _ = _make_service()
    client.list_actions_error = RuntimeError("down")

    tools = await service.list_tools()

    assert [t["name"] for t in tools] == [t["name"] for t in catalog.BUILTIN_TOOLS]


async def test_list_tools_falls_back_without_a_schema() -> None:
    """An action the WM cannot describe is still callable under its own name."""
    service, client, _ = _make_service()
    client.actions = [_action()]
    client.schemas = {}

    tools = await service.list_tools()

    probe = next(t for t in tools if t["name"] == "probe_action")
    assert (
        probe["description"] == "Run probe_action on a project or the whole workspace"
    )


async def test_call_tool_list_projects_passes_the_wm_result_through() -> None:
    """Listing projects reports what the WM holds, unchanged."""
    service, _, _ = _make_service()
    outcome = await service.call_tool("list_projects", {})

    assert isinstance(outcome, ToolData)
    assert outcome.value == {"projects": [{"name": "p", "path": "/probe"}]}


async def test_call_tool_resource_usage_forwards_the_flag() -> None:
    """The cost snapshot honors the opt-in process walk."""
    service, _, _ = _make_service()
    outcome = await service.call_tool("get_resource_usage", {"includeProcesses": True})

    assert isinstance(outcome, ToolData)
    assert outcome.value == {"snapshot": "stub", "includeProcesses": True}


async def test_reload_action_without_a_name_fails_after_connecting() -> None:
    """The missing-argument error is a WM-connected refusal, not a parse miss."""
    service, client, _ = _make_service()
    outcome = await service.call_tool("reload_action", {})

    assert outcome == ToolFailure("Missing 'action' argument")
    assert len(client.connect_calls) == 1


async def test_reload_action_resolves_through_the_cached_mapping() -> None:
    """A listed action is addressed by its source, not its display name."""
    service, client, _ = _make_service()
    client.actions = [_action()]
    client.schemas = {"probe.Action": {"properties": {}, "required": []}}
    await service.list_tools()

    outcome = await service.call_tool("reload_action", {"action": "probe_action"})

    assert isinstance(outcome, ToolData)
    resolved = typing.cast(dict[str, typing.Any], outcome.value)
    assert resolved["action_source"] == "probe.Action"


async def test_resolve_action_source_refreshes_once_then_passes_through() -> None:
    """A tool from a cached list this process never built still resolves.

    An unknown name may already be a source; the WM judges that, not us.
    """
    service, client, _ = _make_service()
    client.actions = [_action()]

    assert await service.resolve_action_source("probe_action") == "probe.Action"
    assert await service.resolve_action_source("probe/unknown") == "probe/unknown"


async def test_reload_config_with_changed_actions_invalidates() -> None:
    """A recovery that added actions clears the cache and tells the client."""
    service, client, recorder = _make_service()
    client.reload_config_result = [
        {
            "project": "/probe",
            "status": "recovered",
            "actionsAdded": ["new"],
            "actionsRemoved": [],
        }
    ]
    service._tool_name_to_source["old"] = "pkg.Old"

    outcome = await service.call_tool("reload_config", {"project": "/probe"})

    assert isinstance(outcome, ToolData)
    assert service._tool_name_to_source == {}
    assert recorder.catalog_changed_calls == 1


async def test_reload_config_without_changes_stays_quiet() -> None:
    """A recovery that only touched handler config refetches nothing."""
    service, _, recorder = _make_service()
    service._tool_name_to_source["lint"] = "pkg.Lint"

    await service.call_tool("reload_config", {"project": "/probe"})

    assert service._tool_name_to_source == {"lint": "pkg.Lint"}
    assert recorder.catalog_changed_calls == 0


async def test_restart_wm_labels_after_reading_info() -> None:
    """The disturbance report names everyone except the caller being replaced.

    The label is read after get_info, while that info is still obtainable.
    """
    order: list[str] = []
    service, client, _ = _make_service()
    client.info_result = {"pid": 7, "clients": ["mcp-test", "mcp-other"]}

    async def _info() -> dict[str, typing.Any]:
        order.append("info")
        return typing.cast(dict[str, typing.Any], client.info_result)

    def _label() -> str:
        order.append("label")
        return "mcp-test"

    async def _replace(c: typing.Any, workdir: pathlib.Path) -> dict[str, typing.Any]:
        order.append("replace")
        assert c is client
        assert workdir == pathlib.Path("/probe")
        return {"port": 9}

    client.get_info = _info
    monkeypatch_label = ToolDependencies(
        connection_config=lambda: WmConnectionConfig(
            client_id="x", can_answer_choices=False
        ),
        current_client_label=_label,
    )
    service._dependencies = monkeypatch_label
    import finecode.mcp_server.core as core_module

    replaced = core_module.wm_lifecycle.replace_running_server
    core_module.wm_lifecycle.replace_running_server = _replace  # type: ignore[method-assign]
    try:
        outcome = await service.call_tool("restart_wm", {})
    finally:
        core_module.wm_lifecycle.replace_running_server = replaced

    assert order == ["info", "label", "replace"]
    assert isinstance(outcome, ToolData)
    assert outcome.value == {
        "restarted": True,
        "previousPid": 7,
        "port": 9,
        "otherClientsDisconnected": ["mcp-other"],
    }


async def test_dump_config_maps_the_format_flag() -> None:
    """`format: false` reaches the action as `format_output: false`.

    The default path leaves the payload unchanged so the action formats.
    """
    service, client, _ = _make_service()
    seen: list[dict[str, typing.Any]] = []

    async def _run(
        action_source: str,
        project: str,
        params: dict[str, typing.Any] | None = None,
        options: dict[str, typing.Any] | None = None,
        **kwargs: typing.Any,
    ) -> dict[str, typing.Any]:
        _ = options
        seen.append({"params": params, "project": project, "source": action_source})
        return {"returnCode": 0}

    client.run_action = _run

    await service.call_tool("dump_config", {"project": "/p", "format": False})
    assert seen[0]["params"]["format_output"] is False

    await service.call_tool("dump_config", {"project": "/p"})
    assert "format_output" not in seen[1]["params"]


async def test_generic_tool_merges_nested_results() -> None:
    """An unknown tool is run by source and its merged result is flattened."""
    service, client, _ = _make_service()
    outcome = await service.call_tool(
        "probe/unknown_tool", {"project": "/probe", "value": "x"}
    )

    assert isinstance(outcome, ToolData)
    merged = typing.cast(dict[str, typing.Any], outcome.value)
    assert merged["resultsByProject"] == {"/probe": {"echo": {"value": "x"}}}
    assert merged["returnCode"] == 0
    assert client.run_action_calls[0]["options"]["mergeResults"] is True


# ---------------------------------------------------------------------------
# Substep 3: streaming queues and merge logic
# ---------------------------------------------------------------------------


async def test_streaming_success_forwards_both_channels_and_cleans_up() -> None:
    """Partials and progress reach the front while the merged result returns."""
    service, client, recorder = _make_service()
    outcome = await service.call_tool(
        "probe_action", {"project": "/probe", "value": "x"}
    )

    assert isinstance(outcome, ToolData)
    assert recorder.partials == [{"partial": 1}]
    assert recorder.progresses == [
        {"type": "progress", "message": "working", "percentage": 50}
    ]
    assert service._partial_result_queues == {}
    assert service._progress_queues == {}
    assert len(client.run_action_calls) == 1


async def test_streaming_wm_error_propagates_without_replay_and_cleans_up() -> None:
    """A run that died is reported, never re-issued, and leaves no queues."""
    service, client, _ = _make_service()
    client.run_action_error = RuntimeError("wm down")

    with pytest.raises(RuntimeError, match="wm down"):
        await service.call_tool("probe_action", {"project": "/probe"})

    assert len(client.run_action_calls) == 1
    assert service._partial_result_queues == {}
    assert service._progress_queues == {}


async def test_streaming_cancellation_cleans_up_queues() -> None:
    """Cancelling a call settles the forwarders and drops both queue entries."""
    service, client, _ = _make_service()
    client.run_action_gate = asyncio.Event()
    task = asyncio.create_task(service.call_tool("probe_action", {"project": "/probe"}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert service._partial_result_queues == {}
    assert service._progress_queues == {}


async def test_none_valued_notifications_are_ignored() -> None:
    """A notification without a value carries nothing to forward."""
    service, client, recorder = _make_service()
    await service.ensure_connected()
    await client.notification_handlers["actions/partialResult"](
        {"token": "missing", "value": None}
    )
    await client.notification_handlers["actions/progress"](
        {"token": "missing", "value": None}
    )

    assert recorder.partials == []
    assert recorder.progresses == []


# ---------------------------------------------------------------------------
# Question conversion (core half of the elicitation path)
# ---------------------------------------------------------------------------


async def test_answered_question_carries_its_value() -> None:
    """An accepted choice answers the run with exactly that choice."""
    service, client, recorder = _make_service()
    await service.ensure_connected()

    reply = await client.request_handlers["client/elicit"](
        {"message": "Continue?", "options": ["yes", "no"], "timeoutSec": 1}
    )

    assert reply == {"outcome": "answered", "value": "yes"}
    assert recorder.questions[0] == Question(
        message="Continue?", options=("yes", "no"), timeout_seconds=1.0
    )


@pytest.mark.parametrize("outcome", ["declined", "unavailable"])
async def test_non_answers_carry_no_value(outcome: str) -> None:
    """Declined and unavailable never smuggle a value onto the wire."""
    service, client, _ = _make_service()
    from finecode.mcp_server.contracts import QuestionOutcome as Outcome

    async def _answer(_question: Question) -> QuestionResult:
        return QuestionResult(Outcome(outcome), "should-not-appear")

    service._effects = ToolEffects(
        partial_result=service._effects.partial_result,
        progress=service._effects.progress,
        question=_answer,
        catalog_changed=service._effects.catalog_changed,
    )
    await service.ensure_connected()

    reply = await client.request_handlers["client/elicit"](
        {"message": "m?", "options": ["a"], "timeoutSec": None}
    )

    assert reply == {"outcome": outcome}
    assert "value" not in reply


async def test_question_effect_error_maps_to_unavailable() -> None:
    """A front that cannot ask is the same to the run as no capability."""
    service, client, recorder = _make_service()
    recorder.question_error = TimeoutError("gone")
    await service.ensure_connected()

    reply = await client.request_handlers["client/elicit"](
        {"message": "m?", "options": ["a"]}
    )

    assert reply == {"outcome": "unavailable"}


async def test_empty_options_are_unavailable_without_asking() -> None:
    """With nothing to offer there is nobody to ask."""
    service, client, recorder = _make_service()
    await service.ensure_connected()

    reply = await client.request_handlers["client/elicit"]({"message": "m?"})

    assert reply == {"outcome": "unavailable"}
    assert recorder.questions == []


# ---------------------------------------------------------------------------
# Substep 4: invalidation (core half of F2)
# ---------------------------------------------------------------------------


async def test_invalidate_clears_before_awaiting_the_effect() -> None:
    """The cache is already empty when the client is told it is stale."""
    service, _, recorder = _make_service()
    service._tool_name_to_source["old"] = "pkg.Old"
    seen: list[dict[str, str]] = []

    async def _changed() -> None:
        seen.append(dict(service._tool_name_to_source))
        recorder.catalog_changed_calls += 1

    service._effects = ToolEffects(
        partial_result=recorder.partial_result,
        progress=recorder.progress,
        question=recorder.question,
        catalog_changed=_changed,
    )

    await service.invalidate_tools()

    assert seen == [{}]


async def test_invalidate_stays_pending_until_notification_completes() -> None:
    """Recovery and re-attach wait for the notification, not a fire-and-forget."""
    service, _, recorder = _make_service()
    recorder.catalog_changed_gate = asyncio.Event()
    task = asyncio.create_task(service.invalidate_tools())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not task.done()
    assert service._tool_name_to_source == {}
    recorder.catalog_changed_gate.set()
    await task

    assert recorder.catalog_changed_calls == 1


async def test_invalidate_failure_propagates_to_the_caller() -> None:
    """A notification that failed fails the recovery the same way."""
    service, _, recorder = _make_service()

    class _Sentinel(Exception):
        pass

    recorder.catalog_changed_error = _Sentinel("send failed")

    with pytest.raises(_Sentinel, match="send failed"):
        await service.invalidate_tools()
