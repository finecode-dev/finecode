"""ADR-0100 requirement tests: the ER-startup gate spans the whole start.

REQUIREMENTS (ADR-0100): the ``er_startup_semaphore`` slot is held from just
before the first ER RPC until the runner reaches RUNNING — not merely from spawn
to RPC connect — so the number of ERs anywhere between spawn and RUNNING is
bounded by ``startup_cap`` (AC1). The slot must never be double-released, must
be released on every exit path exactly once, and a start that holds it must
never leave the runner wedged in INITIALIZING or a waiter parked on
``initialized_event`` forever (AC4). A back-channel request that can wait on
another runner yields the holder's slot first, and the classification that
decides whether a method yields is complete and tested (AC2, AC3). A start that
ends before RUNNING leaves no process and releases the slot (AC4).
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import re

import pytest
from loguru import logger

import finecode_jsonrpc

# Imported for its install side effect: fills ``run_dispatch_bridge``'s slot
# with the real getActionsForParent handler, which AC2 drives (see
# services/run_service/__init__.py).
from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_manager
from finecode.wm_server.services import (
    run_service as _run_service_install,  # noqa: F401
)


class _FakeJsonRpcClient:
    """Stand-in for ``JsonRpcClient``: no process is spawned, but the start and
    kill lifecycle is modelled so failure paths can be exercised.

    ``force_kill()`` latches ``_kill_requested``, and ``start()`` honours the
    latch by raising ``RunnerFailedToStart`` with ``killed_on_spawn`` recorded —
    the way ``_spawn_and_record`` surfaces ``ServerExitedBeforePort`` after a
    kill that arrived while the start was still queued. Feature handlers are
    recorded so a test can drive them (AC2)."""

    instances: list["_FakeJsonRpcClient"] = []
    wait_forever: bool = False
    spawn_wait_sec: float = 0.0

    def __init__(self, *, message_types, readable_id, tracing=None) -> None:
        self.readable_id = readable_id
        self.pid = None
        self.server_exit_callback = None
        self.force_kill_called = False
        self.start_entered = asyncio.Event()
        self.feature_impls: dict[str, object] = {}
        self._kill_requested = False
        self.killed_on_spawn = False
        self.startup_timeline = finecode_jsonrpc.StartupTimeline()
        type(self).instances.append(self)

    async def start(self, **_kwargs) -> None:
        self.start_entered.set()
        if self._kill_requested:
            self.killed_on_spawn = True
            raise runner_manager.RunnerFailedToStart("killed on spawn")
        if type(self).wait_forever:
            await asyncio.Event().wait()
            return
        if type(self).spawn_wait_sec:
            await asyncio.sleep(type(self).spawn_wait_sec)

    def force_kill(self) -> None:
        self.force_kill_called = True
        self._kill_requested = True

    def feature(self, name, impl) -> None:
        self.feature_impls[name] = impl


class _MinimalWmBridge:
    """Stand-in for the log-forwarding bridge the success path touches once at
    the very end (``push_er_forwarding_to_runner``)."""

    async def push_er_forwarding_to_runner(self, runner) -> None: ...


def _make_context(
    tmp_path: pathlib.Path, env_name: str = "e1"
) -> tuple[context.WorkspaceContext, domain.ResolvedProject]:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="a",
        handler_env=env_name,
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    # Pretend the raw config is already known so the start path does not try to
    # read it from disk (the preset-resolution branch is exercised elsewhere).
    ws_context.ws_projects_raw_configs[project.dir_path] = {}
    # Keep `client.start` from starting a real IO thread.
    ws_context.runner_io_thread = object()  # type: ignore[assignment]
    return ws_context, project


async def _patch_start_environment(
    monkeypatch: pytest.MonkeyPatch, cap: int = 1, ws_context=None
) -> None:
    _FakeJsonRpcClient.instances = []
    _FakeJsonRpcClient.wait_forever = False
    _FakeJsonRpcClient.spawn_wait_sec = 0.0
    monkeypatch.setattr(
        runner_manager.finecode_cmd, "get_python_cmd", lambda *a, **k: "fake-python"
    )
    monkeypatch.setattr(
        runner_manager, "_make_runner_client", _tracked_make_runner_client
    )
    if ws_context is not None:
        ws_context.er_startup_semaphore = asyncio.Semaphore(cap)


def _tracked_make_runner_client(runner) -> _FakeJsonRpcClient:
    """``_make_runner_client`` stand-in, with the runner attached to the fake so
    tests can identify each client's runner (instance order alone is not safe
    once starts nest: AC2 starts runner B from inside runner A's update)."""
    fake = _FakeJsonRpcClient(message_types=None, readable_id=runner.readable_id)
    fake.runner_ref = runner
    return fake


async def _patch_success_init(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok_init(runner, project):
        return None

    class _RunnerInfo:
        log_file_path = None

    async def _get_runner_info(client):
        return _RunnerInfo()

    async def _ok_update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        return None

    async def _ok_finish(runner, project, ws_context):
        return None

    async def _noop_project_changed(project) -> None: ...

    monkeypatch.setattr(runner_manager, "_init_lsp_client", _ok_init)
    monkeypatch.setattr(
        runner_manager._internal_client_api, "get_runner_info", _get_runner_info
    )
    monkeypatch.setattr(runner_manager, "update_runner_config", _ok_update_config)
    monkeypatch.setattr(runner_manager, "_finish_runner_init", _ok_finish)
    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)
    monkeypatch.setattr(
        runner_manager.wm_bridge, "handlers", lambda: _MinimalWmBridge()
    )


def _runner_from_context(
    ws_context: context.WorkspaceContext, project: domain.Project, env_name: str
) -> object:
    return ws_context.ws_projects_extension_runners[project.dir_path][env_name]


# --------------------------------------------------------------------------- #
# 1.1 — _StartupSlot primitive
# --------------------------------------------------------------------------- #


async def test_startup_slot_release_is_idempotent() -> None:
    """An early back-channel yield plus the context-manager exit must release
    the slot exactly once — a double release silently raises the cap, and
    ``asyncio.Semaphore`` turns it into a loud ``ValueError``."""
    semaphore = asyncio.Semaphore(2)
    slot = runner_manager._StartupSlot(semaphore)
    async with slot:
        slot.release()  # early yield, as a back-channel handler would
        # __aexit__ calls release() again; without idempotence this raises
    assert semaphore._value == 2


async def test_startup_slot_cancel_while_acquiring_holds_no_slot() -> None:
    """A start cancelled before the slot was granted must not consume it — the
    counter is the liveness the runner's state assertions check end-to-end."""
    semaphore = asyncio.Semaphore(1)
    holder = runner_manager._StartupSlot(semaphore)
    await holder.__aenter__()

    queued = runner_manager._StartupSlot(semaphore)
    task = asyncio.create_task(queued.__aenter__())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert semaphore._value == 0  # the queued acquire never took a slot
    await holder.__aexit__(None, None, None)
    assert semaphore._value == 1


# --------------------------------------------------------------------------- #
# AC1 — the gate is used and never exceeded across the whole start span
# --------------------------------------------------------------------------- #


async def test_er_startup_spans_to_running_and_is_bounded_by_cap(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peak overlap of [start() entered, runner RUNNING] must be exactly the
    cap: no more ERs may be anywhere between spawn and RUNNING at once, and the
    cap must actually be reached (a gate that over-serialises would show 1)."""
    cap = 2
    runner_count = 6
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    projects = []
    for i in range(runner_count):
        project_dir = tmp_path / f"project_{i}"
        project_dir.mkdir()
        project = wm_testing.make_single_action_project(
            dir_path=project_dir, action_name="a", handler_env="e1"
        )
        projects.append(project)
        ws_context.ws_projects[project.dir_path] = project
        ws_context.ws_projects_raw_configs[project.dir_path] = {}
    ws_context.runner_io_thread = object()  # type: ignore[assignment]

    await _patch_start_environment(monkeypatch, cap=cap, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    _FakeJsonRpcClient.spawn_wait_sec = 0.05

    active = 0
    max_observed = 0

    tasks = [
        asyncio.create_task(
            runner_manager._start_runner(
                project_def=project,
                env_name="e1",
                handlers_to_initialize=None,
                ws_context=ws_context,
            )
        )
        for project in projects
    ]
    # Let every task reach its first await so each client exists and is in
    # context (construction is synchronous, before the slot acquire).
    await asyncio.sleep(0)

    async def _observe(i: int) -> None:
        nonlocal active, max_observed
        client = _FakeJsonRpcClient.instances[i]
        await client.start_entered.wait()
        active += 1
        max_observed = max(max_observed, active)
        try:
            runner = ws_context.ws_projects_extension_runners[projects[i].dir_path][
                "e1"
            ]
            await runner.initialized_event.wait()
        finally:
            active -= 1

    observers = [asyncio.create_task(_observe(i)) for i in range(runner_count)]
    await asyncio.gather(*tasks)
    await asyncio.gather(*observers)

    assert len(_FakeJsonRpcClient.instances) == runner_count
    assert max_observed == cap
    assert ws_context.er_startup_semaphore._value == cap
    for project in projects:
        runner = _runner_from_context(ws_context, project, "e1")
        assert runner.status == domain.ExtensionRunnerStatus.RUNNING


# --------------------------------------------------------------------------- #
# AC2 — the real back-channel cycle is broken by the yield
# --------------------------------------------------------------------------- #


def _install_ac2_update_config(
    monkeypatch: pytest.MonkeyPatch, project: domain.Project
) -> None:
    async def _update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        if runner.env_name == "e1":
            # Runner A: its registered getActionsForParent handler runs the real
            # resolution chain, which starts B (env e2).
            impl = runner.client.feature_impls[
                _internal_client_types.GET_ACTIONS_FOR_PARENT
            ]
            params = _internal_client_types.GetActionsForParentParams(
                parent_action_source="test.actions.TestAction"
            )
            await impl(params)
        else:
            # Runner B: resolve the action's metadata so the resolution completes.
            for action in project.actions:
                action.canonical_source = "test.actions.TestAction"
        return None

    monkeypatch.setattr(runner_manager, "update_runner_config", _update_config)


async def test_get_actions_for_parent_yields_the_slot_and_completes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `getActionsForParent` request that reaches `ensure_action_metadata`
    while its runner holds the startup slot must yield the slot, or runner B's
    on-demand start would queue forever on A's own slot. Completion alone is not
    evidence — `find_subactions_for_parent` swallows errors from
    `ensure_action_metadata` — so the test also asserts B reached RUNNING, the
    action got its canonical_source, and a yield record was emitted."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="a", handler_env="e2"
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    ws_context.ws_projects_raw_configs[project.dir_path] = {}
    ws_context.runner_io_thread = object()  # type: ignore[assignment]
    ws_context.er_startup_semaphore = asyncio.Semaphore(1)
    await _patch_start_environment(monkeypatch, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    _install_ac2_update_config(monkeypatch, project)

    records: list[str] = []
    sink_id = logger.add(
        lambda message: records.append(message), level="DEBUG", format="{message}"
    )
    try:
        async with asyncio.timeout(5):
            await runner_manager._start_runner(
                project_def=project,
                env_name="e1",
                handlers_to_initialize=None,
                ws_context=ws_context,
            )
    finally:
        logger.remove(sink_id)

    a = _runner_from_context(ws_context, project, "e1")
    b = _runner_from_context(ws_context, project, "e2")
    assert a.status == domain.ExtensionRunnerStatus.RUNNING
    assert b.status == domain.ExtensionRunnerStatus.RUNNING
    assert all(action.canonical_source is not None for action in project.actions)
    assert any(
        "startup slot yielded" in message
        and _internal_client_types.GET_ACTIONS_FOR_PARENT in message
        for message in records
    )
    assert ws_context.er_startup_semaphore._value == 1


async def test_without_yield_same_cycle_hangs_on_the_slot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falsifying variant of the AC2 cycle: with the yield wrapper disabled, the
    same test deadlocks — the semaphore is exhausted (A holds the only slot) and
    B's client was never spawned. That ties the hang to the slot and to nothing
    else: ``er_startup_semaphore._value == 0`` and B was never started."""
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="a", handler_env="e2"
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    ws_context.ws_projects_raw_configs[project.dir_path] = {}
    ws_context.runner_io_thread = object()  # type: ignore[assignment]
    ws_context.er_startup_semaphore = asyncio.Semaphore(1)
    await _patch_start_environment(monkeypatch, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    _install_ac2_update_config(monkeypatch, project)
    monkeypatch.setattr(
        runner_manager, "_yield_startup_slot", lambda runner, method, **kwargs: None
    )

    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )
    async with asyncio.timeout(5):
        while "e2" not in ws_context.ws_projects_extension_runners.get(
            project.dir_path, {}
        ):
            await asyncio.sleep(0.01)
        b = _runner_from_context(ws_context, project, "e2")
        while b.status != domain.ExtensionRunnerStatus.INITIALIZING:
            await asyncio.sleep(0.01)
        # The deadlock is live: A holds the slot, B is queued behind it.
        assert ws_context.er_startup_semaphore._value == 0
        b_client = next(
            client
            for client in _FakeJsonRpcClient.instances
            if getattr(client, "runner_ref", None) is b
        )
        assert not b_client.start_entered.is_set()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(1):
                await task

    # Both abandon paths ran: every waiter released, the slot is back.
    assert ws_context.er_startup_semaphore._value == 1
    assert a_and_b_events_set(ws_context, project, "e1", "e2")


def a_and_b_events_set(ws_context, project, *envs) -> bool:
    return all(
        runner.initialized_event.is_set()
        for env in envs
        for runner in [ws_context.ws_projects_extension_runners[project.dir_path][env]]
    )


# --------------------------------------------------------------------------- #
# AC3 — every registration is classified, in exactly one set
# --------------------------------------------------------------------------- #


def test_every_registered_startup_method_is_classified() -> None:
    """Every method ``_start_extension_runner_process`` registers must be in
    exactly one of the two classification sets. An unclassified method would
    fail open at runtime (yield + WARNING), widening the gate silently; the test
    names it."""
    source = inspect.getsource(runner_manager._start_extension_runner_process)
    registered = re.findall(r"_register\(\s*_internal_client_types\.([A-Z_]+)", source)
    assert registered, "no registrations found — is the source shape still right?"
    for name in registered:
        method = getattr(_internal_client_types, name)
        is_yielding = method in runner_manager._STARTUP_SLOT_YIELDING_METHODS
        is_neutral = method in runner_manager._STARTUP_SLOT_NEUTRAL_METHODS
        assert is_yielding != is_neutral, (
            f"startup method {name} must be in exactly one of"
            " _STARTUP_SLOT_YIELDING_METHODS / _STARTUP_SLOT_NEUTRAL_METHODS"
        )


# --------------------------------------------------------------------------- #
# AC4 — every exit path releases the slot exactly once and leaves no wedge
# --------------------------------------------------------------------------- #


def _assert_slot_and_runner_state(
    ws_context: context.WorkspaceContext,
    runner,
    cap: int,
    status: domain.ExtensionRunnerStatus,
) -> None:
    assert ws_context.er_startup_semaphore._value == cap
    assert runner.status != domain.ExtensionRunnerStatus.INITIALIZING
    assert runner.initialized_event.is_set()
    assert runner.status == status


async def test_success_releases_the_slot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    runner = await runner_manager._start_runner(
        project_def=project,
        env_name="e1",
        handlers_to_initialize=None,
        ws_context=ws_context,
    )
    assert runner.status == domain.ExtensionRunnerStatus.RUNNING
    assert ws_context.er_startup_semaphore._value == 1


async def test_init_failure_releases_the_slot_and_marks_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)

    async def _fail_init(runner, project):
        raise runner_manager.RunnerFailedToStart("initialize failed")

    monkeypatch.setattr(runner_manager, "_init_lsp_client", _fail_init)

    with pytest.raises(runner_manager.RunnerFailedToStart):
        await runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    runner = _runner_from_context(ws_context, project, "e1")
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.FAILED
    )
    assert _FakeJsonRpcClient.instances[0].force_kill_called


async def test_update_config_failure_releases_the_slot_and_keeps_the_type(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)

    async def _fail_update_config(
        *, runner, project, handlers_to_initialize, ws_context, pass_label="other"
    ):
        raise runner_manager.EnvironmentOutOfDateError(
            "environment is stale", env_name=runner.env_name
        )

    monkeypatch.setattr(runner_manager, "update_runner_config", _fail_update_config)

    with pytest.raises(runner_manager.EnvironmentOutOfDateError):
        await runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    runner = _runner_from_context(ws_context, project, "e1")
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.FAILED
    )


async def test_cancel_while_queued_abandons_with_client_attached(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start cancelled before the slot was granted must still end in FAILED
    with the event set (ADR-0097), and the pre-attached client must never have
    been spawned — the kill latch worked only because the client existed."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)

    await ws_context.er_startup_semaphore.acquire()  # hold the slot

    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )
    while "e1" not in ws_context.ws_projects_extension_runners.get(
        project.dir_path, {}
    ):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws_context.er_startup_semaphore.release()

    runner = _runner_from_context(ws_context, project, "e1")
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.FAILED
    )
    assert runner.client is not None
    assert not runner.client.start_entered.is_set()


async def test_cancel_while_holding_releases_the_slot(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    _FakeJsonRpcClient.wait_forever = True

    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )
    async with asyncio.timeout(5):
        while (
            not _FakeJsonRpcClient.instances
            or not _FakeJsonRpcClient.instances[0].start_entered.is_set()
        ):
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    runner = _runner_from_context(ws_context, project, "e1")
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.FAILED
    )


async def test_early_yield_then_normal_exit_does_not_double_release(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A back-channel yield during the start followed by a normal exit must not
    over-release the semaphore (which raises ``ValueError`` and would fail this
    test) — the release handle is idempotent."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)
    # Hold start() open so the yielded-handle window is observable, then let the
    # start run to completion after the early yield.
    _FakeJsonRpcClient.spawn_wait_sec = 0.5

    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )
    runner = None
    async with asyncio.timeout(5):
        while "e1" not in ws_context.ws_projects_extension_runners.get(
            project.dir_path, {}
        ):
            await asyncio.sleep(0.01)
        runner = _runner_from_context(ws_context, project, "e1")
        while runner.startup_slot_release is None:
            await asyncio.sleep(0.01)
        assert ws_context.er_startup_semaphore._value == 0
        runner.startup_slot_release()  # early yield
        await task

    assert runner.status == domain.ExtensionRunnerStatus.RUNNING
    assert ws_context.er_startup_semaphore._value == 1


async def test_kill_while_queued_spawns_nothing_and_marks_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force-killing a queued runner (the ADR-0097 shutdown sweep) must latch,
    so the spawn that finally happens raises like the real
    ``ServerExitedBeforePort`` path — the runner ends FAILED with the event set,
    and no OS process was ever started."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    await _patch_success_init(monkeypatch)

    await ws_context.er_startup_semaphore.acquire()  # hold the slot
    task = asyncio.create_task(
        runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    )
    runner = None
    async with asyncio.timeout(5):
        while "e1" not in ws_context.ws_projects_extension_runners.get(
            project.dir_path, {}
        ):
            await asyncio.sleep(0.01)
        runner = _runner_from_context(ws_context, project, "e1")
        # The client is attached before the slot, so its presence marks the
        # runner as queued; `startup_slot_release` stays None until the slot is
        # granted, which happens only after the force_kill below.
        while runner.client is None:
            await asyncio.sleep(0.01)
        client = runner.client
        assert client is not None
        assert not client.start_entered.is_set()
        client.force_kill()
    assert ws_context.er_startup_semaphore._value == 0
    ws_context.er_startup_semaphore.release()  # grant the slot

    with pytest.raises(runner_manager.RunnerFailedToStart):
        await task

    assert client.killed_on_spawn
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.FAILED
    )


async def test_no_venv_releases_the_slot_and_stays_no_venv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NO_VENV status set under the slot must survive the abandon path
    (which flips only INITIALIZING), and the constructed-but-never-started
    client must not have been spawned."""
    ws_context, project = _make_context(tmp_path)
    await _patch_start_environment(monkeypatch, cap=1, ws_context=ws_context)
    monkeypatch.setattr(
        runner_manager.finecode_cmd,
        "get_python_cmd",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("venv not found")),
    )

    async def _noop_project_changed(project) -> None: ...

    monkeypatch.setattr(runner_manager, "notify_project_changed", _noop_project_changed)

    with pytest.raises(runner_manager.RunnerFailedToStart):
        await runner_manager._start_runner(
            project_def=project,
            env_name="e1",
            handlers_to_initialize=None,
            ws_context=ws_context,
        )
    runner = _runner_from_context(ws_context, project, "e1")
    _assert_slot_and_runner_state(
        ws_context, runner, 1, domain.ExtensionRunnerStatus.NO_VENV
    )
    assert runner.client is not None
    assert not runner.client.start_entered.is_set()


# --------------------------------------------------------------------------- #
# AC4b — preset resolution never touches an unconnected dev_workspace runner
# --------------------------------------------------------------------------- #


async def test_preset_resolution_refuses_unconnected_dev_workspace_runner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dev_workspace start that reaches the preset-resolution branch while
    the project's dev_workspace runner is still queued (or has no client) must
    fail with a named start error rather than an AttributeError deep inside
    resolvePackagePath — and must not wait on that runner (a wait would be a
    real slot cycle, since the waiter holds a slot)."""
    await _patch_start_environment(monkeypatch, cap=1)
    await _patch_success_init(monkeypatch)
    project_dir = tmp_path / "preset_project"
    project_dir.mkdir()
    # The check sits after the config read, so the project needs a definition.
    (project_dir / "pyproject.toml").write_text('[project]\nname = "preset_project"\n')
    project = wm_testing.make_single_action_project(
        dir_path=project_dir, action_name="a", handler_env="e2"
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[project_dir])
    ws_context.ws_projects[project.dir_path] = project
    # Deliberately no raw config: that is what routes the start into the
    # preset-resolution branch.
    ws_context.runner_io_thread = object()  # type: ignore[assignment]

    for client, setup_name in (
        (None, "client is None"),
        (
            _FakeJsonRpcClient(message_types=None, readable_id="dev_workspace"),
            "unconnected",
        ),
    ):
        ws_context.ws_projects_extension_runners[project.dir_path] = {}
        dev_workspace = runner_manager.runner_client.ExtensionRunnerInfo(
            working_dir_path=project.dir_path,
            env_name="dev_workspace",
            status=domain.ExtensionRunnerStatus.INITIALIZING,
        )
        dev_workspace.client = client
        ws_context.ws_projects_extension_runners[project.dir_path]["dev_workspace"] = (
            dev_workspace
        )

        with pytest.raises(runner_manager.RunnerFailedToStart) as excinfo:
            await runner_manager._start_runner(
                project_def=project,
                env_name="e2",
                handlers_to_initialize=None,
                ws_context=ws_context,
            )
        assert "dev_workspace" in str(excinfo.value)
        assert "has not connected yet" in str(excinfo.value)
