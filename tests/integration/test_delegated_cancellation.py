"""Integration: cancelling a run cancels the runs it delegated (#67).

Real WM + ER composition on the opt-in wire path. A parent in
``dev_workspace`` delegates to a ticking cooperative leaf in
``dev_no_runtime``; cancelling the top-level run (client disconnect) or only
the parent's own await (sibling failure) cancels the leaf within
``DELEGATED_STOP_WAIT_SECONDS`` and the parent reads ``STOPPED``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import shutil
import sys
import sysconfig
import time
import typing
import uuid

import pytest

from finecode.wm_server import domain
from finecode.wm_server.services import in_flight_runs, shutdown_service

FIXTURE_MODULE = "delegated_cancellation_probe"
B = 5.0


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _repair_copied_venv_path(
    venv_dir: pathlib.Path, old_venv_dir: pathlib.Path
) -> None:
    import sys as _sys

    if _sys.platform == "win32":
        activate_path = venv_dir / "Scripts" / "activate.bat"
    else:
        activate_path = venv_dir / "bin" / "activate"
    content = activate_path.read_text(encoding="utf-8")
    activate_path.write_text(
        content.replace(str(old_venv_dir), str(venv_dir)), encoding="utf-8"
    )


def _copy_env(src: pathlib.Path, dst: pathlib.Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> list[str]:
        ignored: list[str] = []
        for name in names:
            if name == "__pycache__" or name.endswith(".pyc"):
                ignored.append(name)
        return ignored

    shutil.copytree(src, dst, symlinks=True, ignore=_ignore)


def _prepare_project(
    tmp_path: pathlib.Path,
    leaf_env: str,
    *,
    project_name: str = "delegated-cancellation-probe",
) -> tuple[pathlib.Path, pathlib.Path]:
    for mod in ("finecode", "finecode_extension_runner", "finecode_extension_api"):
        try:
            __import__(mod)
        except ImportError as exc:
            raise RuntimeError(
                f"fixture setup failed: required module {mod!r} not importable "
                f"in {sys.executable}: {exc}"
            ) from exc
    source_env = pathlib.Path(sys.executable).parent.parent
    project_dir = tmp_path / "project"
    fixture_modules = tmp_path / "fixture_modules"
    project_dir.mkdir(parents=True, exist_ok=True)
    fixture_modules.mkdir(parents=True, exist_ok=True)
    shutil.copy(_repo_fixture_module(), fixture_modules / f"{FIXTURE_MODULE}.py")
    venvs_dir = project_dir / ".venvs"
    venvs_dir.mkdir(exist_ok=True)
    _copy_env(source_env, venvs_dir / "dev_workspace")
    _repair_copied_venv_path(venvs_dir / "dev_workspace", source_env)
    if leaf_env != "dev_workspace":
        _copy_env(source_env, venvs_dir / leaf_env)
        _repair_copied_venv_path(venvs_dir / leaf_env, source_env)
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in {"dev_workspace", leaf_env}:
        purelib = venvs_dir / env_name / purelib_relative
        purelib.mkdir(parents=True, exist_ok=True)
        (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            str(fixture_modules) + "\n", encoding="utf-8"
        )
    project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "{project_name}"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
dev_no_runtime = ["finecode_extension_runner"]

[tool.finecode.action.probe_parent]
source = "{FIXTURE_MODULE}.ProbeParentAction"
handlers = [{{ name = "parent", source = "{FIXTURE_MODULE}.ProbeParentHandler", env = "dev_workspace" }}]

[tool.finecode.action.probe_leaf]
source = "{FIXTURE_MODULE}.ProbeLeafAction"
handlers = [{{ name = "leaf", source = "{FIXTURE_MODULE}.ProbeLeafHandler", env = "{leaf_env}" }}]
""",
        encoding="utf-8",
    )
    return project_dir, fixture_modules


def _read_events(log_path: pathlib.Path) -> list[dict]:
    events: list[dict] = []
    if not log_path.exists():
        return events
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


async def _wait_for(
    predicate: typing.Callable[[], bool],
    *,
    timeout: float,
    interval: float = 0.05,
    on_timeout: typing.Callable[[], str] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            extra = f"\n{on_timeout()}" if on_timeout is not None else ""
            raise AssertionError(f"timed out waiting for condition{extra}")
        await asyncio.sleep(interval)


def _event_time(events: list[dict], who: str, what: str) -> float | None:
    for event in events:
        if event.get("who") == who and event.get("what") == what:
            return float(event["t"])
    return None


def _has_event(events: list[dict], who: str, what: str) -> bool:
    return _event_time(events, who, what) is not None


async def _setup_one_project(
    wm_client: typing.Any, tmp_path: pathlib.Path, leaf_env: str = "dev_no_runtime"
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    project_dir, _ = _prepare_project(tmp_path, leaf_env)
    await wm_client.request(
        "client/initialize", {"clientId": f"deleg-cancel-{uuid.uuid4().hex}"}
    )
    added = await wm_client.request(
        "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
    )
    assert added is not None
    ctx = wm_client.ws_context
    project = ctx.ws_projects.get(project_dir)
    assert project is not None
    assert project.status == domain.ProjectStatus.CONFIG_VALID
    runners = ctx.ws_projects_extension_runners.get(project_dir, {})
    assert runners, "real runners were not started"
    log_path = project_dir / f"events-{uuid.uuid4().hex}.jsonl"
    outcome_path = project_dir / f"outcome-{uuid.uuid4().hex}.txt"
    return project_dir, log_path, outcome_path


def _parent_params(
    project_dir: pathlib.Path,
    log_path: pathlib.Path,
    outcome_path: pathlib.Path,
    *,
    delegation: str = "run_action",
    leaf_mode: str = "normal",
    parent_behavior: str = "passthrough",
    nest: int = 0,
    tick_here: bool = False,
    lender_path: str = "",
    token: str | None = None,
) -> dict[str, typing.Any]:
    params: dict[str, typing.Any] = {
        "actionSource": f"{FIXTURE_MODULE}.ProbeParentAction",
        "project": str(project_dir),
        "params": {
            "log_path": str(log_path),
            "outcome_path": str(outcome_path),
            "delegation": delegation,
            "leaf_mode": leaf_mode,
            "parent_behavior": parent_behavior,
            "nest": nest,
            "tick_here": tick_here,
            "lender_path": lender_path,
        },
        "options": {"resultFormats": ["json"], "trigger": "user", "devEnv": "ci"},
    }
    if token is not None:
        params["partialResultToken"] = token
    return params


@pytest.mark.parametrize("delegation", ["run_action", "run_action_iter"])
async def test_top_level_cancel_stops_delegated_child(
    wm_client: typing.Any, tmp_path: pathlib.Path, delegation: str
) -> None:
    """Cancelling the top-level streamed run cancels the delegated leaf (A1).

    The leaf sees the cancellation and stops within the bound; the parent's
    await raises ``CancelledError`` (never ``ActionRunCancelled``) and the
    parent reads ``STOPPED``. Without the fix the leaf would run to its end.
    """
    from tests.integration.conftest import InProcClient

    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context
    run_task: asyncio.Future | None = None
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    runner_client: InProcClient | None = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
        runner_client = InProcClient(reader, writer, ctx)
        await runner_client.request(
            "client/initialize",
            {"clientId": f"deleg-cancel-top-{uuid.uuid4().hex}"},
            timeout=60.0,
        )
        token = f"deleg-top-{uuid.uuid4().hex}"
        run_task = asyncio.ensure_future(
            runner_client.request(
                "actions/run",
                _parent_params(
                    project_dir,
                    log_path,
                    outcome_path,
                    delegation=delegation,
                    token=token,
                ),
                timeout=60.0,
            )
        )
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "leaf", "tick"),
            timeout=55.0,
            on_timeout=lambda: (
                f"events={_read_events(log_path)!r}\n"
                f"run_done={run_task.done() if run_task else None} "
                f"outcome_exists={outcome_path.exists()}\n"
                f"in_flight={[(r.action_name, r.run_id) for r in in_flight_runs.runs_in_project(ctx, project_dir)]!r}"
            ),
        )
        cancel_at = time.time()
        run_task.cancel()
        await runner_client.close()
        runner_client = None
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "leaf", "cancel_seen"),
            timeout=10.0,
        )
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "parent", "cancelled"),
            timeout=B + 2.0,
        )
        events = _read_events(log_path)
        leaf_cancel = _event_time(events, "leaf", "cancel_seen")
        leaf_end = _event_time(events, "leaf", "cleanup_end")
        parent_cancel = _event_time(events, "parent", "cancelled")
        assert leaf_cancel is not None and parent_cancel is not None
        assert leaf_end is not None, "cooperative leaf did not stop"
        assert leaf_end - cancel_at < B
        assert parent_cancel - cancel_at < B
        for event in events:
            if event.get("who") == "parent" and event.get("what") == "cancelled":
                # WmCallAbandoned is the ER-internal CancelledError carrying
                # the stop verdict; either spelling is a cancellation, never
                # ActionRunCancelled.
                assert event.get("exc_type") in ("CancelledError", "WmCallAbandoned")
                assert event.get("exc_type") != "ActionRunCancelled"
        await _wait_for(outcome_path.exists, timeout=5.0)
        assert outcome_path.read_text(encoding="utf-8") == "stopped"
        assert "ActionRunCancelled" not in outcome_path.read_text(encoding="utf-8")
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_sibling_failure_cancels_only_parent_await(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Only the parent's await is cancelled; the top-level run returns (A2).

    A sibling in the parent's ``TaskGroup`` raises after 1 s; the parent
    catches the group and returns a result. The leaf is cancelled as in A1
    and the parent reads ``STOPPED``.
    """
    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context
    try:
        token = f"deleg-sibling-{uuid.uuid4().hex}"
        result = await wm_client.request(
            "actions/run",
            _parent_params(
                project_dir,
                log_path,
                outcome_path,
                delegation="run_action",
                parent_behavior="sibling",
                token=token,
            ),
            timeout=60.0,
        )
        assert result["returnCode"] == 0
        events = _read_events(log_path)
        assert _has_event(events, "leaf", "cancel_seen")
        assert _has_event(events, "leaf", "cleanup_end")
        assert _has_event(events, "parent", "sibling_caught")
        assert outcome_path.read_text(encoding="utf-8") == "stopped"
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_workspace_cancel_stops_both_project_children(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Cancelling a workspace fan-out cancels both project children (A3).

    The parent fans out across two projects; the sibling failure cancels the
    fan-out await. The await raises ``CancelledError`` inside the handler
    (never ``ActionRunFailed``) and the read is ``STOPPED``.
    """
    project_dirs: list[pathlib.Path] = []
    log_paths: list[pathlib.Path] = []
    outcome_paths: list[pathlib.Path] = []
    ctx = wm_client.ws_context
    try:
        await wm_client.request(
            "client/initialize", {"clientId": f"deleg-ws-{uuid.uuid4().hex}"}
        )
        for index in range(2):
            sub = tmp_path / f"p{index}"
            sub.mkdir(parents=True, exist_ok=True)
            project_dir, _ = _prepare_project(
                sub, "dev_no_runtime", project_name=f"deleg-ws-{index}"
            )
            # _prepare_project nests under sub/project; use the real dir.
            project_dirs.append(project_dir)
            added = await wm_client.request(
                "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
            )
            assert added is not None
        log_path = project_dirs[0] / f"events-{uuid.uuid4().hex}.jsonl"
        outcome_path = project_dirs[0] / f"outcome-{uuid.uuid4().hex}.txt"
        log_paths.append(log_path)
        outcome_paths.append(outcome_path)
        # Point both projects' leaves at the same log via per-project payload
        # overrides: the parent fans out with one payload, so use the shared
        # log path in that payload.
        token = f"deleg-ws-{uuid.uuid4().hex}"
        result = await wm_client.request(
            "actions/run",
            _parent_params(
                project_dirs[0],
                log_path,
                outcome_path,
                delegation="workspace",
                parent_behavior="sibling",
                token=token,
            ),
            timeout=60.0,
        )
        assert result["returnCode"] == 0
        events = _read_events(log_path)
        cancel_seen = [e for e in events if e.get("what") == "cancel_seen"]
        assert len(cancel_seen) >= 2, f"both children must cancel: {events}"
        assert outcome_path.read_text(encoding="utf-8") == "stopped"
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_early_close_cancels_child_on_real_path(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """An early close of ``run_action_iter`` ends the child (A5).

    On the WM path the close cancels the child, completes within the bound,
    never raises, and the call counts in the read.
    """
    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context
    try:
        token = f"deleg-close-{uuid.uuid4().hex}"
        started = time.monotonic()
        result = await wm_client.request(
            "actions/run",
            _parent_params(
                project_dir,
                log_path,
                outcome_path,
                delegation="early_close",
                token=token,
            ),
            timeout=60.0,
        )
        elapsed = time.monotonic() - started
        assert result["returnCode"] == 0
        assert elapsed < B
        events = _read_events(log_path)
        assert _has_event(events, "leaf", "cancel_seen")
        assert _has_event(events, "parent", "end")
        assert outcome_path.read_text(encoding="utf-8") == "stopped"
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_stubborn_leaf_makes_parent_await_end_unconfirmed(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A stubborn leaf makes the parent's await end at B unconfirmed (A13).

    The leaf ignores the cancel for B + 1 s; the parent's await ends at B
    (± 0.5 s) with ``CancelledError`` and the read is ``UNCONFIRMED``.
    """
    from tests.integration.conftest import InProcClient

    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context
    run_task: asyncio.Future | None = None
    runner_client: InProcClient | None = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
        runner_client = InProcClient(reader, writer, ctx)
        await runner_client.request(
            "client/initialize",
            {"clientId": f"deleg-stubborn-{uuid.uuid4().hex}"},
            timeout=60.0,
        )
        token = f"deleg-stubborn-{uuid.uuid4().hex}"
        run_task = asyncio.ensure_future(
            runner_client.request(
                "actions/run",
                _parent_params(
                    project_dir,
                    log_path,
                    outcome_path,
                    delegation="run_action",
                    leaf_mode="stubborn",
                    token=token,
                ),
                timeout=60.0,
            )
        )
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "leaf", "tick"),
            timeout=55.0,
        )
        await runner_client.close()
        runner_client = None
        if run_task is not None:
            run_task.cancel()
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "parent", "cancelled"),
            timeout=B + 2.0,
        )
        events = _read_events(log_path)
        leaf_cancel = _event_time(events, "leaf", "cancel_seen")
        parent_cancel = _event_time(events, "parent", "cancelled")
        assert leaf_cancel is not None and parent_cancel is not None
        waited = parent_cancel - leaf_cancel
        assert B - 0.5 <= waited <= B + 0.5, f"waited={waited}"
        await _wait_for(outcome_path.exists, timeout=5.0)
        assert outcome_path.read_text(encoding="utf-8") == "unconfirmed"
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_nested_stubborn_grandchild_reads_unconfirmed(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A nested stubborn grandchild reads unconfirmed within the bound (A13).

    ``dev_workspace → dev_no_runtime → dev_workspace`` with a stubborn
    grandchild: the child's cancelled reply carries ``subtreeStopped: false``,
    the parent reads ``UNCONFIRMED``, and the parent's await still ends
    within B (± 0.5 s).
    """
    from tests.integration.conftest import InProcClient

    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context
    run_task: asyncio.Future | None = None
    runner_client: InProcClient | None = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
        runner_client = InProcClient(reader, writer, ctx)
        await runner_client.request(
            "client/initialize",
            {"clientId": f"deleg-nested-{uuid.uuid4().hex}"},
            timeout=60.0,
        )
        token = f"deleg-nested-{uuid.uuid4().hex}"
        run_task = asyncio.ensure_future(
            runner_client.request(
                "actions/run",
                _parent_params(
                    project_dir,
                    log_path,
                    outcome_path,
                    delegation="run_action",
                    leaf_mode="stubborn",
                    nest=2,
                    token=token,
                ),
                timeout=60.0,
            )
        )
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "parent_tick", "tick"),
            timeout=55.0,
        )
        await runner_client.close()
        runner_client = None
        if run_task is not None:
            run_task.cancel()
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "parent", "cancelled"),
            timeout=B + 2.0,
        )
        events = _read_events(log_path)
        middle_cancel = _event_time(events, "leaf", "cancel_seen")
        parent_cancel = _event_time(events, "parent", "cancelled")
        assert parent_cancel is not None
        if middle_cancel is not None:
            waited = parent_cancel - middle_cancel
            assert waited <= B + 0.5, f"waited={waited}"
        await _wait_for(outcome_path.exists, timeout=5.0)
        assert outcome_path.read_text(encoding="utf-8") == "unconfirmed"
        grandchild_ticks = [
            e
            for e in _read_events(log_path)
            if e.get("who") == "parent_tick" and e.get("what") == "stubborn_tick"
        ]
        assert grandchild_ticks, "stubborn grandchild kept ticking past the bound"
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_recovery_window_while_parent_unwinds(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Recovery of the parent's project while it unwinds within B (A17).

    A lender parent in dev_workspace lends a temp file to a leaf in
    dev_no_runtime; the leaf checks the file every tick and ignores the cancel
    for 1.5 s; the parent deletes the file 1 s after its await ended, on
    STOPPED. (i) An ordinary reload right after the top-level cancel is
    refused, naming the child. (ii) A reload after the child replied but
    before the parent deleted the file is not refused on this run's account.
    (iii) An override reload right after the cancel replaces the runners. In
    every case the child never observed the file missing while it ran.
    """
    from tests.integration.conftest import InProcClient

    project_dir, log_path, outcome_path = await _setup_one_project(wm_client, tmp_path)
    ctx = wm_client.ws_context

    async def _start_run(
        lender_path: pathlib.Path, log: pathlib.Path, outcome: pathlib.Path
    ) -> tuple[InProcClient, asyncio.Future]:
        reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
        client = InProcClient(reader, writer, ctx)
        await client.request(
            "client/initialize",
            {"clientId": f"deleg-lender-{uuid.uuid4().hex}"},
            timeout=60.0,
        )
        token = f"deleg-lender-{uuid.uuid4().hex}"
        task = asyncio.ensure_future(
            client.request(
                "actions/run",
                _parent_params(
                    project_dir,
                    log,
                    outcome,
                    delegation="run_action",
                    leaf_mode="lender",
                    lender_path=str(lender_path),
                    token=token,
                ),
                timeout=60.0,
            )
        )
        return client, task

    # Run A: ordinary reloads (i) refused during flight, (ii) ok after reply.
    lender_a = project_dir / f"lent-{uuid.uuid4().hex}"
    lender_a.write_text("lent\n", encoding="utf-8")
    log_a = project_dir / f"events-a-{uuid.uuid4().hex}.jsonl"
    outcome_a = project_dir / f"outcome-a-{uuid.uuid4().hex}.txt"
    runner_client, run_task = await _start_run(lender_a, log_a, outcome_a)
    try:
        await _wait_for(
            lambda: _has_event(_read_events(log_a), "leaf", "tick"),
            timeout=55.0,
        )
        run_task.cancel()
        await runner_client.close()
        # (i) Ordinary reload right after the cancel is refused, naming child.
        refused = await wm_client.request(
            "workspace/reloadConfig",
            {"project": str(project_dir)},
            timeout=60.0,
        )
        first = refused["projects"][0]
        assert first["status"] == "refused", first
        assert any(run.get("action") == "probe_leaf" for run in first["inFlight"]), (
            first
        )
        # (ii) After the child replied but before the parent deleted the file:
        # the child is gone from flight and the reload is not refused.
        await _wait_for(
            lambda: _has_event(_read_events(log_a), "leaf", "cleanup_end"),
            timeout=10.0,
        )
        await _wait_for(
            lambda: _has_event(_read_events(log_a), "parent", "cancelled"),
            timeout=B + 2.0,
        )
        await _wait_for(
            lambda: (
                not any(
                    run.get("action") == "probe_leaf"
                    for run in in_flight_runs.runs_in_project(ctx, project_dir)
                )
            ),
            timeout=10.0,
        )
        assert lender_a.exists(), "parent deleted the lent file too early"
        recovered = await wm_client.request(
            "workspace/reloadConfig",
            {"project": str(project_dir)},
            timeout=60.0,
        )
        assert recovered["projects"][0]["status"] != "refused", recovered
        # The recovery restarts the runners, so the sleeping parent never
        # reaches its delayed delete; the file stays and the child never saw
        # it missing.
        assert lender_a.exists()
        assert not any(e.get("what") == "lender_missing" for e in _read_events(log_a))
    finally:
        with contextlib.suppress(Exception):
            await runner_client.close()
        if not run_task.done():
            run_task.cancel()

    # Run C: no reload interference — the parent deletes the lent file 1 s
    # after its await ended, on STOPPED.
    lender_c = project_dir / f"lent-{uuid.uuid4().hex}"
    lender_c.write_text("lent\n", encoding="utf-8")
    log_c = project_dir / f"events-c-{uuid.uuid4().hex}.jsonl"
    outcome_c = project_dir / f"outcome-c-{uuid.uuid4().hex}.txt"
    runner_client_c, run_task_c = await _start_run(lender_c, log_c, outcome_c)
    try:
        await _wait_for(
            lambda: _has_event(_read_events(log_c), "leaf", "tick"),
            timeout=55.0,
        )
        run_task_c.cancel()
        await runner_client_c.close()
        await _wait_for(
            lambda: _has_event(_read_events(log_c), "parent", "lender_deleted"),
            timeout=10.0,
        )
        assert not lender_c.exists()
        assert outcome_c.read_text(encoding="utf-8") == "stopped"
        assert not any(e.get("what") == "lender_missing" for e in _read_events(log_c))
    finally:
        with contextlib.suppress(Exception):
            await runner_client_c.close()
        if not run_task_c.done():
            run_task_c.cancel()

    # Run B: override reload (iii) right after the cancel replaces runners.
    lender_b = project_dir / f"lent-{uuid.uuid4().hex}"
    lender_b.write_text("lent\n", encoding="utf-8")
    log_b = project_dir / f"events-b-{uuid.uuid4().hex}.jsonl"
    outcome_b = project_dir / f"outcome-b-{uuid.uuid4().hex}.txt"
    before = {
        env: id(runner)
        for env, runner in ctx.ws_projects_extension_runners.get(
            project_dir, {}
        ).items()
    }
    runner_client_b, run_task_b = await _start_run(lender_b, log_b, outcome_b)
    try:
        await _wait_for(
            lambda: _has_event(_read_events(log_b), "leaf", "tick"),
            timeout=55.0,
        )
        run_task_b.cancel()
        await runner_client_b.close()
        overridden = await wm_client.request(
            "workspace/reloadConfig",
            {"project": str(project_dir), "killInFlightRuns": True},
            timeout=60.0,
        )
        assert overridden["projects"][0]["status"] == "recovered", overridden
        after = {
            env: id(runner)
            for env, runner in ctx.ws_projects_extension_runners.get(
                project_dir, {}
            ).items()
        }
        assert before and after and before != after
        assert not any(e.get("what") == "lender_missing" for e in _read_events(log_b))
    finally:
        with contextlib.suppress(Exception):
            await runner_client_b.close()
        if not run_task_b.done():
            run_task_b.cancel()
        await shutdown_service.on_shutdown(ctx)
