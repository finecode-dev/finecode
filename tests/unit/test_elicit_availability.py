"""``finecode/elicitAvailability`` answers from the WM's own facts (D6.check).

The availability probe must agree with ``finecode/elicit`` on every cause:
no origin, a gone client, nothing declared, or no installed bridge all read
as "cannot" — and none of them sends anything to a client. A nested run
reports its root client's availability.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest
from finecode_jsonrpc._converter import converter

import finecode_jsonrpc
from finecode.wm_server import context, errors, wm_server
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_manager,
)
from finecode.wm_server.services.run_service import er_dispatch

_CANONICAL_SOURCE = "test.actions.TestAction"
_OUTER_RUN = "run-outer"


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    elicitation_bridge.reset_origins()


class _FakeWriter:
    """Captures what the server writes; stands in for a client connection."""

    def __init__(self) -> None:
        self.written: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        raise AssertionError("tests capture through _write_message instead")

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str) -> str:
        return "test-peer"


class _RecordingClient:
    """Stands in for ``JsonRpcClient``; records back-channel registrations."""

    def __init__(self, **_kwargs: object) -> None:
        self.readable_id = "fake-er"
        self.feature_impls: dict[str, object] = {}
        self.pid: int | None = None
        self.server_exit_callback: object = None
        self.startup_timeline = finecode_jsonrpc.StartupTimeline()

    def feature(self, name: str, impl: object) -> None:
        self.feature_impls[name] = impl

    def feature_sync(self, name: str, impl: object) -> None:
        pass

    async def start(self, **_kwargs: object) -> None:
        return None

    def force_kill(self) -> None:
        return None

    async def send_request(
        self, method: str, params: object = None, **_kwargs: object
    ) -> typing.Any:
        raise AssertionError(f"unexpected send_request {method}")

    def notify(self, method: str, params: object = None) -> None: ...


def _bridge() -> wm_server._WmElicitationBridge:
    return wm_server._WmElicitationBridge()


async def test_no_origin_means_cannot() -> None:
    """A run nobody started has nobody to ask — answered without a round trip."""
    assert _bridge().can_elicit(run_writer_key=None) is False


async def test_a_gone_client_means_cannot() -> None:
    """A connection the registry no longer holds was disconnected mid-run."""
    assert _bridge().can_elicit(run_writer_key=_FakeWriter()) is False


async def test_an_undeclared_client_means_cannot() -> None:
    """Connected but declared nothing (a CI client): known before any send."""
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)

    assert _bridge().can_elicit(run_writer_key=writer) is False


async def test_a_capable_connected_client_can_be_asked() -> None:
    """The one case that reads "can": connected and declared."""
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)
    wm_server._client_capabilities[writer] = {"elicitation": {"choice": True}}

    assert _bridge().can_elicit(run_writer_key=writer) is True


async def _started_handler(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> typing.Any:
    """The real ``handle_elicit_availability`` as registered on a start."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="a", handler_env="e1"
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    ws_context.ws_projects_raw_configs[project.dir_path] = {}
    ws_context.runner_io_thread = object()  # type: ignore[assignment]
    ws_context.er_startup_semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(
        runner_manager.finecode_cmd, "get_python_cmd", lambda *a, **k: "fake-python"
    )
    monkeypatch.setattr(
        runner_manager, "_make_runner_client", lambda runner: _RecordingClient()
    )

    async def _ok_init(runner, project, ws_context=None):
        return None

    async def _ok_update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        return None

    async def _ok_finish(runner, project, ws_context):
        return None

    async def _noop_project_changed(project) -> None: ...

    async def _ok_runner_info(client):
        return None

    class _MinimalWmBridge:
        async def push_er_forwarding_to_runner(self, runner) -> None: ...

    monkeypatch.setattr(runner_manager, "_init_lsp_client", _ok_init)
    monkeypatch.setattr(
        runner_manager._internal_client_api, "get_runner_info", _ok_runner_info
    )
    monkeypatch.setattr(runner_manager, "update_runner_config", _ok_update_config)
    monkeypatch.setattr(runner_manager, "_finish_runner_init", _ok_finish)
    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)
    monkeypatch.setattr(
        runner_manager.wm_bridge, "handlers", lambda: _MinimalWmBridge()
    )
    runner = await runner_manager._start_runner(
        project_def=project,
        env_name="e1",
        handlers_to_initialize=None,
        ws_context=ws_context,
    )
    assert runner.client is not None
    impl = runner.client.feature_impls[_internal_client_types.ELICIT_AVAILABILITY]
    assert _internal_client_types.ELICIT_AVAILABILITY in (
        runner_manager._STARTUP_SLOT_NEUTRAL_METHODS
    )
    return impl


async def test_no_installed_bridge_is_an_error_not_a_guess(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing installed means the WM cannot answer at all — the ER reads that
    failure as "cannot" rather than the WM inventing an answer."""
    impl = await _started_handler(tmp_path, monkeypatch)
    monkeypatch.setattr(elicitation_bridge, "handlers", lambda: None)

    with pytest.raises(errors.InternalError):
        await impl(_internal_client_types.ElicitAvailabilityParams(run_id="run-1"))


async def test_the_handler_answers_from_the_origin_registry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered handler resolves the run's origin and delegates to the
    installed bridge — the same facts ``elicit`` checks before sending."""
    impl = await _started_handler(tmp_path, monkeypatch)
    seen: list[object | None] = []

    class _StubBridge:
        def can_elicit(self, *, run_writer_key: object | None) -> bool:
            seen.append(run_writer_key)
            return run_writer_key is not None

    monkeypatch.setattr(elicitation_bridge, "handlers", lambda: _StubBridge())
    terminal = object()
    with elicitation_bridge.bind_run(
        "run-1", elicitation_bridge.RunDispatchOrigin(connection=terminal)
    ):
        result = await impl(
            _internal_client_types.ElicitAvailabilityParams(run_id="run-1")
        )
    assert result == _internal_client_types.ElicitAvailabilityResult(can_answer=True)
    assert seen == [terminal]

    unbound = await impl(
        _internal_client_types.ElicitAvailabilityParams(run_id="run-unknown")
    )
    assert unbound == _internal_client_types.ElicitAvailabilityResult(can_answer=False)


class _AskingErClient(wm_testing.FakeErClient):
    """Records the run id the WM hands to the nested ER."""

    def __init__(self) -> None:
        super().__init__()
        self.run_ids: list[str | None] = []
        self.availability_while_running: list[bool] = []

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
    ) -> typing.Any:
        run_id = (getattr(params, "options", None) or {}).get("runId")
        self.run_ids.append(run_id)
        installed = elicitation_bridge.handlers()
        assert installed is not None
        self.availability_while_running.append(
            installed.can_elicit(
                run_writer_key=elicitation_bridge.originating_client_for_run(run_id)
            )
        )
        return await super().send_request(method, params, timeout)


def _project_with_runner(dir_path: pathlib.Path, client=None):
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    er_client = client if client is not None else _AskingErClient()
    er_client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    runner = wm_testing.make_running_runner(working_dir_path=dir_path, client=er_client)
    return project, runner, er_client


def _workspace(*projects) -> object:
    first_project, first_runner = projects[0]
    ws_context = wm_testing.make_workspace_context(
        project=first_project, runner=first_runner
    )
    for project, runner in projects[1:]:
        ws_context.ws_projects[project.dir_path] = project
        ws_context.ws_projects_extension_runners[project.dir_path] = {
            "test_env": runner
        }
    return ws_context


def _dispatch_params(project_paths: list[str], run_id: str | None):
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        project_paths=project_paths,
        run_id=run_id,
    )


@pytest.mark.parametrize("capable", [True, False])
async def test_a_nested_run_reports_its_root_client(
    tmp_path: pathlib.Path, capable: bool
) -> None:
    """D6.G5: the nested run is bound to the root connection, so the probe
    answers with the root client's availability — no new code per hop."""
    started_in = tmp_path / "started_in"
    fanned_into = tmp_path / "fanned_into"
    project, runner, _ = _project_with_runner(started_in)
    other_project, other_runner, other_client = _project_with_runner(fanned_into)
    ws_context = _workspace((project, runner), (other_project, other_runner))

    terminal = _FakeWriter()
    wm_server._connected_clients.add(terminal)
    if capable:
        wm_server._client_capabilities[terminal] = {"elicitation": {"choice": True}}

    with elicitation_bridge.bind_run(
        _OUTER_RUN, elicitation_bridge.RunDispatchOrigin(connection=terminal)
    ):
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_dispatch_params([str(fanned_into)], _OUTER_RUN),
            ws_context=ws_context,
        )

    nested_run_id = other_client.run_ids[0]
    assert nested_run_id != _OUTER_RUN
    assert other_client.availability_while_running == [capable]


def test_the_new_request_is_registered_for_the_wire() -> None:
    """Same gate as ``finecode/elicit``: absent from the table the client
    drops it and the ER would always read "method not found"."""
    entry = _internal_client_types.METHOD_TO_TYPES[
        _internal_client_types.ELICIT_AVAILABILITY
    ]
    assert entry[0] is _internal_client_types.ElicitAvailabilityRequest
    assert entry[1] is _internal_client_types.ElicitAvailabilityParams
    assert entry[2] is _internal_client_types.ElicitAvailabilityResponse
    assert entry[3] is _internal_client_types.ElicitAvailabilityResult

    request = converter.structure(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "finecode/elicitAvailability",
            "params": {"runId": "run-1"},
        },
        entry[0],
    )
    assert request.params.run_id == "run-1"
    assert converter.unstructure(
        _internal_client_types.ElicitAvailabilityResult(can_answer=True)
    ) == {"canAnswer": True}
