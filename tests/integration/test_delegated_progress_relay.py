"""Delegated actions relay live progress to parent handlers (real processes).

Uses the in-process WM TCP fixture with real ER subprocesses from copied
testing venvs. No runner or dispatch mocking. The root client observes
``actions/progress`` on the root run's token; WM-side observers record
releases and parent-bound notifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import shutil
import sys
import sysconfig
import typing
import uuid

import pytest

from finecode.wm_server import domain, wal
from finecode.wm_server.runner import elicitation_bridge
from finecode.wm_server.services import in_flight_runs, shutdown_service
from finecode.wm_server.services.run_service import er_dispatch

FIXTURE_MODULE = "delegated_progress_probe"


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
    tmp_path: pathlib.Path, leaf_env: str
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
            f"{fixture_modules}\n", encoding="utf-8"
        )
    leaf = leaf_env
    project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "delegated-progress-probe"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
dev_no_runtime = ["finecode_extension_runner"]

[tool.finecode.action.probe_parent]
source = "{FIXTURE_MODULE}.ProbeParentAction"
handlers = [{{ name = "parent", source = "{FIXTURE_MODULE}.ProbeParentHandler", env = "dev_workspace" }}]

[tool.finecode.action.probe_leaf]
source = "{FIXTURE_MODULE}.ProbeLeafAction"
handlers = [{{ name = "leaf", source = "{FIXTURE_MODULE}.ProbeLeafHandler", env = "{leaf}" }}]

[tool.finecode.action.probe_streaming_leaf]
source = "{FIXTURE_MODULE}.ProbeStreamingLeafAction"
handlers = [{{ name = "streaming_leaf", source = "{FIXTURE_MODULE}.ProbeStreamingLeafHandler", env = "{leaf}" }}]
""",
        encoding="utf-8",
    )
    return project_dir, fixture_modules


def _payload(**overrides: typing.Any) -> dict:
    params: dict = {
        "release_path": "",
        "message": "",
        "delegations": 0,
        "scenario": "hold",
        "script": [],
        "flood_count": 0,
        "streaming_partials": 0,
        "with_progress": True,
        "parent_close": "none",
        "slow_pull_seconds": 0.0,
        "fanout": 1,
        "flaky_sink": False,
    }
    params.update(overrides)
    return params


async def _setup_project(
    wm_client: typing.Any, tmp_path: pathlib.Path, leaf_env: str
) -> tuple[pathlib.Path, typing.Any]:
    project_dir, _ = _prepare_project(tmp_path, leaf_env)
    ctx = wm_client.ws_context
    ctx.wal_writer = None
    await wm_client.request(
        "client/initialize",
        {"clientId": f"delegated-progress-{uuid.uuid4().hex}"},
        timeout=60.0,
    )
    add_result = await wm_client.request(
        "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
    )
    assert add_result is not None
    project = ctx.ws_projects.get(project_dir)
    assert project is not None
    assert project.status == domain.ProjectStatus.CONFIG_VALID
    runners = ctx.ws_projects_extension_runners.get(project_dir, {})
    assert runners, "real runners were not started"
    for runner in runners.values():
        assert runner.status.name == "RUNNING"
    return project_dir, ctx


def _start_root_run(
    wm_client: typing.Any,
    project_dir: pathlib.Path,
    params: dict,
    token: str,
    action: str = f"{FIXTURE_MODULE}.ProbeParentAction",
) -> asyncio.Task:
    return asyncio.ensure_future(
        wm_client.request(
            "actions/run",
            {
                "actionSource": action,
                "project": str(project_dir),
                "params": params,
                "progressToken": token,
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
            },
            timeout=60.0,
        )
    )


async def _wait_for_report(
    wm_client: typing.Any,
    token: str,
    message: str,
    collected: list[dict],
    timeout: float = 55.0,
    tasks: tuple[asyncio.Task | None, ...] = (),
    pending: list[dict] | None = None,
) -> dict:
    """Wait for a report, tolerating silent gaps and other tokens' traffic.

    Notifications for other tokens are stashed in *pending* (shared per test)
    instead of dropped, so concurrent waits on one client do not eat each
    other's reports. A failing waited-for task surfaces immediately.
    """
    buf = pending if pending is not None else []
    for queued in list(buf):
        if queued.get("token") != token:
            continue
        value = queued.get("value", {})
        collected.append(value)
        if value.get("type") == "report" and value.get("message") == message:
            buf.remove(queued)
            return value
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for task in tasks:
            if task is not None and task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    raise exc
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(
                f"progress report {message!r} not received for {token!r}"
            )
        try:
            params = await wm_client.next_notification(
                "actions/progress", timeout=min(2.0, remaining)
            )
        except TimeoutError:
            continue
        if params.get("token") != token:
            buf.append(params)
            continue
        value = params.get("value", {})
        collected.append(value)
        if value.get("type") == "report" and value.get("message") == message:
            return value


async def _collect_until_end(
    wm_client: typing.Any,
    token: str,
    collected: list[dict],
    timeout: float = 20.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"progress end not received for {token!r}")
        try:
            params = await wm_client.next_notification(
                "actions/progress", timeout=min(2.0, remaining)
            )
        except TimeoutError:
            continue
        if params.get("token") != token:
            continue
        value = params.get("value", {})
        collected.append(value)
        if value.get("type") == "end":
            return


@contextlib.asynccontextmanager
async def _record_releases() -> typing.AsyncGenerator[list, None]:
    """Record WM-side release calls, delegating to the real handler."""
    calls: list = []
    orig = er_dispatch._BridgeHandlers.release_delegated_call

    async def _wrapper(
        self: typing.Any, runner: typing.Any, params: typing.Any, ws_context: typing.Any
    ) -> typing.Any:
        calls.append(params.partial_result_token)
        return await orig(self, runner, params, ws_context)

    er_dispatch._BridgeHandlers.release_delegated_call = _wrapper  # type: ignore[method-assign]
    try:
        yield calls
    finally:
        er_dispatch._BridgeHandlers.release_delegated_call = orig  # type: ignore[method-assign]


@contextlib.asynccontextmanager
async def _watch_parent_notifications(
    ctx: typing.Any, project_dir: pathlib.Path, parent_env: str = "dev_workspace"
) -> typing.AsyncGenerator[list, None]:
    """Record every WM→parent notification on the parent connection."""
    runner = ctx.ws_projects_extension_runners.get(project_dir, {}).get(parent_env)
    assert runner is not None
    sent: list = []
    orig = runner.client.notify_and_drain

    async def _wrapper(method: str, params: typing.Any = None) -> typing.Any:
        sent.append((method, params))
        return await orig(method, params)

    runner.client.notify_and_drain = _wrapper
    try:
        yield sent
    finally:
        runner.client.notify_and_drain = orig


async def _finish_run(
    ctx: typing.Any,
    project_dir: pathlib.Path,
    task: asyncio.Task | None,
    *paths: pathlib.Path,
) -> None:
    for path in paths:
        try:
            if not path.exists():
                path.write_text("go\n", encoding="utf-8")
        except OSError:
            pass
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await shutdown_service.on_shutdown(ctx)
    ctx.wal_writer = None


@pytest.mark.parametrize("leaf_env", ["dev_workspace", "dev_no_runtime"])
@pytest.mark.parametrize("wal_enabled", [False, True])
async def test_live_child_report_reaches_root_before_child_ends(
    wm_client: typing.Any,
    tmp_path: pathlib.Path,
    leaf_env: str,
    wal_enabled: bool,
) -> None:
    """A live child report reaches the root run while the child is held."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, leaf_env)
    writer: wal.WalWriter | None = None
    if wal_enabled:
        writer = wal.WalWriter(
            wal.WalConfig(enabled=True, dir_path=tmp_path / "wm-wal")
        )
        ctx.wal_writer = writer
    else:
        ctx.wal_writer = None

    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    nonce = f"nonce-{uuid.uuid4().hex}"
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    root_task: asyncio.Task | None = None
    try:
        root_task = _start_root_run(
            wm_client,
            project_dir,
            _payload(release_path=str(release_path), message=nonce),
            token,
        )
        await _wait_for_report(wm_client, token, nonce, collected, timeout=55.0)
        assert root_task is not None and not root_task.done()

        runs = in_flight_runs.runs_in_project(ctx, project_dir)
        by_action = {run.action_name: run for run in runs}
        assert "probe_parent" in by_action
        assert "probe_leaf" in by_action
        parent_run = by_action["probe_parent"]
        leaf_run = by_action["probe_leaf"]
        assert parent_run.run_id and leaf_run.run_id
        assert parent_run.run_id != leaf_run.run_id

        parent_origin = elicitation_bridge.originating_client_for_run(parent_run.run_id)
        assert parent_origin is not None
        leaf_origin = elicitation_bridge.originating_client_for_run(leaf_run.run_id)
        assert leaf_origin is parent_origin

        reload_result = await wm_client.request(
            "workspace/reloadConfig", {"project": str(project_dir)}
        )
        assert reload_result["projects"][0]["status"] == "refused"
        refused_ids = {run["runId"] for run in reload_result["projects"][0]["inFlight"]}
        assert parent_run.run_id in refused_ids
        assert leaf_run.run_id in refused_ids

        release_path.write_text("go\n", encoding="utf-8")
        result = await asyncio.wait_for(root_task, timeout=60.0)
        assert result["returnCode"] == 0
        result_json = result["resultByFormat"]["json"]
        assert result_json.get("depths") == [1, 2]

        if wal_enabled:
            records: list[dict] = []
            for path in sorted((tmp_path / "wm-wal").glob("wal-*.jsonl")):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        records.append(json.loads(line))
            child_records = [
                r for r in records if r.get("wal_run_id") == leaf_run.run_id
            ]
            assert child_records, "no WAL records for child run"
            child_types = [r.get("event_type") for r in child_records]
            assert child_types == [
                "run.accepted",
                "runner.selected",
                "run.dispatched",
                "run.completed",
            ]
            assert all(r.get("action_name") == "probe_leaf" for r in child_records)

        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        begins = [v for v in collected if v.get("type") == "begin"]
        ends = [v for v in collected if v.get("type") == "end"]
        assert len(begins) == 1
        assert len(ends) == 1
        reports = [v for v in collected if v.get("type") == "report"]
        assert any(r.get("message") == nonce for r in reports)
    finally:
        if writer is not None:
            writer.close()
        await _finish_run(ctx, project_dir, root_task, release_path)


@pytest.mark.parametrize("leaf_env", ["dev_workspace", "dev_no_runtime"])
@pytest.mark.parametrize("kind", ["hold", "streaming", "raise"])
async def test_delegated_outcome_matches_no_progress(
    wm_client: typing.Any, tmp_path: pathlib.Path, leaf_env: str, kind: str
) -> None:
    """Progress relay changes neither partials, results nor errors (AC2)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, leaf_env)

    async def _run_once(with_progress: bool) -> tuple[dict | None, str | None]:
        release_path = project_dir / f"release-{uuid.uuid4().hex}"
        token = f"root-{uuid.uuid4().hex}"
        if kind == "hold":
            params = _payload(
                release_path=str(release_path),
                message=f"m-{uuid.uuid4().hex}",
                with_progress=with_progress,
            )
        elif kind == "streaming":
            params = _payload(
                release_path=str(release_path),
                streaming_partials=3,
                with_progress=with_progress,
            )
        else:
            params = _payload(
                release_path=str(release_path),
                scenario="raise",
                with_progress=with_progress,
            )
        if kind != "raise":
            release_path.write_text("go\n", encoding="utf-8")
        task = _start_root_run(wm_client, project_dir, params, token)
        try:
            result = await asyncio.wait_for(task, timeout=60.0)
            return result, None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        finally:
            try:
                if not release_path.exists():
                    release_path.write_text("go\n", encoding="utf-8")
            except OSError:
                pass

    result_on, error_on = await _run_once(True)
    result_off, error_off = await _run_once(False)
    try:
        if kind == "raise":
            assert error_on is not None and error_off is not None
            assert "leaf failed" in error_on
            assert "leaf failed" in error_off
        else:
            assert error_on is None and error_off is None
            assert result_on is not None and result_off is not None
            assert result_on["returnCode"] == result_off["returnCode"] == 0
            json_on = result_on["resultByFormat"]["json"]
            json_off = result_off["resultByFormat"]["json"]
            assert json_on == json_off
            if kind == "streaming":
                assert json_on["texts"] == ["part-0", "part-1", "part-2"]
    finally:
        await _finish_run(ctx, project_dir, None)


async def test_concurrent_delegations_are_isolated(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Two parent ERs with equal tokens never see each other's progress (AC3)."""
    project_a, _ = _prepare_project(tmp_path / "a", "dev_no_runtime")
    project_b, _ = _prepare_project(tmp_path / "b", "dev_no_runtime")
    ctx = wm_client.ws_context
    ctx.wal_writer = None
    await wm_client.request(
        "client/initialize", {"clientId": "delegated-progress-ac3"}, timeout=60.0
    )
    for project_dir in (project_a, project_b):
        add_result = await wm_client.request(
            "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
        )
        assert add_result is not None
    task_a: asyncio.Task | None = None
    task_b: asyncio.Task | None = None
    try:
        release_a = project_a / f"release-{uuid.uuid4().hex}"
        release_b = project_b / f"release-{uuid.uuid4().hex}"
        release_a.write_text("go\n", encoding="utf-8")
        release_b.write_text("go\n", encoding="utf-8")
        nonce_a = f"nonce-a-{uuid.uuid4().hex}"
        nonce_b = f"nonce-b-{uuid.uuid4().hex}"
        token_a = f"root-a-{uuid.uuid4().hex}"
        token_b = f"root-b-{uuid.uuid4().hex}"
        collected_a: list[dict] = []
        collected_b: list[dict] = []
        shared_pending: list[dict] = []
        task_a = _start_root_run(
            wm_client,
            project_a,
            _payload(release_path=str(release_a), message=nonce_a),
            token_a,
        )
        task_b = _start_root_run(
            wm_client,
            project_b,
            _payload(release_path=str(release_b), message=nonce_b),
            token_b,
        )
        await _wait_for_report(
            wm_client,
            token_a,
            nonce_a,
            collected_a,
            tasks=(task_a, task_b),
            pending=shared_pending,
        )
        await _wait_for_report(
            wm_client,
            token_b,
            nonce_b,
            collected_b,
            tasks=(task_a, task_b),
            pending=shared_pending,
        )
        messages_a = [
            v.get("message") for v in collected_a if v.get("type") == "report"
        ]
        messages_b = [
            v.get("message") for v in collected_b if v.get("type") == "report"
        ]
        assert nonce_a in messages_a and nonce_b not in messages_a
        assert nonce_b in messages_b and nonce_a not in messages_b
        result_a = await asyncio.wait_for(task_a, timeout=60.0)
        result_b = await asyncio.wait_for(task_b, timeout=60.0)
        assert result_a["returnCode"] == result_b["returnCode"] == 0
        assert er_dispatch._delegated_calls == {}
    finally:
        await _finish_run(ctx, project_a, task_a)
        await _finish_run(ctx, project_b, task_b)


async def test_close_releases_forwarding_child_runs_on(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Closing after the first partial releases forwarding; the child ends (D8.G1)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    async with _record_releases() as releases:
        async with _watch_parent_notifications(ctx, project_dir) as parent_sent:
            release_path = project_dir / f"release-{uuid.uuid4().hex}"
            token = f"root-{uuid.uuid4().hex}"
            task = _start_root_run(
                wm_client,
                project_dir,
                _payload(
                    release_path=str(release_path),
                    message=f"m-{uuid.uuid4().hex}",
                    streaming_partials=2,
                    parent_close="after_first",
                ),
                token,
            )
            try:
                result = await asyncio.wait_for(task, timeout=60.0)
                assert result["returnCode"] == 0
                for _ in range(1000):
                    if releases:
                        break
                    await asyncio.sleep(0.01)
                assert releases, "expected a release for the early close"
                sent_at_release = len(parent_sent)
                assert sent_at_release > 0
                # The child still runs to its end after the release reply.
                release_path.write_text("go\n", encoding="utf-8")
                await asyncio.sleep(2.0)
                late = [
                    (method, params)
                    for method, params in parent_sent[sent_at_release:]
                    if method == "$/progress"
                ]
                assert late == []
                assert er_dispatch._delegated_calls == {}
            finally:
                await _finish_run(ctx, project_dir, task, release_path)


async def test_cancel_releases_forwarding(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Cancelling after the first partial releases forwarding (D8.G2)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    async with _record_releases() as releases:
        release_path = project_dir / f"release-{uuid.uuid4().hex}"
        token = f"root-{uuid.uuid4().hex}"
        task = _start_root_run(
            wm_client,
            project_dir,
            _payload(
                release_path=str(release_path),
                message=f"m-{uuid.uuid4().hex}",
                streaming_partials=2,
                parent_close="cancel_after_first",
            ),
            token,
        )
        try:
            try:
                await asyncio.wait_for(task, timeout=60.0)
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            else:
                raise AssertionError("expected the cancelled run to fail")
            assert releases, "expected a release for the cancellation"
            assert er_dispatch._delegated_calls == {}
        finally:
            await _finish_run(ctx, project_dir, task, release_path)


async def test_two_calls_one_released(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Two concurrent calls release independently; the other is unaffected (D8.G7)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    async with _record_releases() as releases:
        release_a = project_dir / f"release-a-{uuid.uuid4().hex}"
        release_b = project_dir / f"release-b-{uuid.uuid4().hex}"
        token_a = f"root-a-{uuid.uuid4().hex}"
        token_b = f"root-b-{uuid.uuid4().hex}"
        collected_b: list[dict] = []
        task_a = _start_root_run(
            wm_client,
            project_dir,
            _payload(
                release_path=str(release_a),
                message=f"a-{uuid.uuid4().hex}",
                streaming_partials=2,
                parent_close="after_first",
            ),
            token_a,
        )
        task_b = _start_root_run(
            wm_client,
            project_dir,
            _payload(
                release_path=str(release_b),
                message=f"b-{uuid.uuid4().hex}",
            ),
            token_b,
        )
        try:
            result_a = await asyncio.wait_for(task_a, timeout=60.0)
            assert result_a["returnCode"] == 0
            assert releases, "expected a release for call A"
            release_b.write_text("go\n", encoding="utf-8")
            result_b = await asyncio.wait_for(task_b, timeout=60.0)
            assert result_b["returnCode"] == 0
            await _collect_until_end(wm_client, token_b, collected_b, timeout=20.0)
            assert any(v.get("type") == "report" for v in collected_b)
            assert er_dispatch._delegated_calls == {}
        finally:
            await _finish_run(ctx, project_dir, task_a, release_a)
            await _finish_run(ctx, project_dir, task_b, release_b)


async def test_release_before_dispatch_never_forwards(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A release gated on dispatch runs the child with no forwarding (D8.G8)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    gate = asyncio.Event()
    gated_calls: list = []
    orig = er_dispatch.ProjectExecutor.run_action_with_partial_results

    @contextlib.asynccontextmanager
    async def _gated(
        self: typing.Any, **kwargs: typing.Any
    ) -> collections.abc.AsyncGenerator[typing.Any, None]:
        gated_calls.append(kwargs.get("action_source"))
        await gate.wait()
        async with orig(self, **kwargs) as inner_ctx:
            yield inner_ctx

    er_dispatch.ProjectExecutor.run_action_with_partial_results = _gated  # type: ignore[method-assign]
    try:
        async with _record_releases() as releases:
            async with _watch_parent_notifications(ctx, project_dir) as parent_sent:
                release_path = project_dir / f"release-{uuid.uuid4().hex}"
                token = f"root-{uuid.uuid4().hex}"
                task = _start_root_run(
                    wm_client,
                    project_dir,
                    _payload(
                        release_path=str(release_path),
                        message=f"m-{uuid.uuid4().hex}",
                        parent_close="immediate",
                    ),
                    token,
                )
                try:
                    for _ in range(1000):
                        if releases:
                            break
                        await asyncio.sleep(0.01)
                    assert releases, "expected the immediate close to release"
                    gate.set()
                    result = await asyncio.wait_for(task, timeout=60.0)
                    assert result["returnCode"] == 0
                    assert gated_calls, "the child dispatch never reached the gate"
                    assert [
                        (method, params)
                        for method, params in parent_sent
                        if method == "$/progress"
                    ] == []
                    release_path.write_text("go\n", encoding="utf-8")
                    for _ in range(900):
                        if not in_flight_runs.runs_in_project(ctx, project_dir):
                            break
                        await asyncio.sleep(0.1)
                    assert in_flight_runs.runs_in_project(ctx, project_dir) == []
                    assert er_dispatch._delegated_calls == {}
                finally:
                    gate.set()
                    await _finish_run(ctx, project_dir, task, release_path)
    finally:
        er_dispatch.ProjectExecutor.run_action_with_partial_results = orig  # type: ignore[method-assign]


async def test_released_child_continuity(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A released call's child keeps its lineage, tracking and WAL lifecycle (5.4)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    writer = wal.WalWriter(wal.WalConfig(enabled=True, dir_path=tmp_path / "wm-wal"))
    ctx.wal_writer = writer
    release_a = project_dir / f"release-a-{uuid.uuid4().hex}"
    token_a = f"root-a-{uuid.uuid4().hex}"
    task_a: asyncio.Task | None = None
    try:
        async with _record_releases() as releases:
            task_a = _start_root_run(
                wm_client,
                project_dir,
                _payload(
                    release_path=str(release_a),
                    message=f"a-{uuid.uuid4().hex}",
                    streaming_partials=2,
                    parent_close="after_first",
                ),
                token_a,
            )
            result_a = await asyncio.wait_for(task_a, timeout=60.0)
            assert result_a["returnCode"] == 0
            assert releases, "expected a release for call A"

            runs = in_flight_runs.runs_in_project(ctx, project_dir)
            leaf_runs = [r for r in runs if r.action_name != "probe_parent"]
            assert leaf_runs, "the released child stays tracked until it ends"
            leaf_run_id = leaf_runs[0].run_id

            reload_result = await wm_client.request(
                "workspace/reloadConfig", {"project": str(project_dir)}
            )
            assert reload_result["projects"][0]["status"] == "refused"
            refused_ids = {
                run["runId"] for run in reload_result["projects"][0]["inFlight"]
            }
            assert leaf_run_id in refused_ids

            release_a.write_text("go\n", encoding="utf-8")
            for _ in range(900):
                if not [
                    r
                    for r in in_flight_runs.runs_in_project(ctx, project_dir)
                    if r.action_name != "probe_parent"
                ]:
                    break
                await asyncio.sleep(0.1)

            child_records: list[dict] = []
            for _ in range(900):
                records: list[dict] = []
                for path in sorted((tmp_path / "wm-wal").glob("wal-*.jsonl")):
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            records.append(json.loads(line))
                child_records = [
                    r for r in records if r.get("wal_run_id") == leaf_run_id
                ]
                if any(r.get("event_type") == "run.completed" for r in child_records):
                    break
                await asyncio.sleep(0.1)
            assert child_records, "no WAL records for the released child run"
            assert child_records[0].get("action_name") in (
                "probe_leaf",
                "probe_streaming_leaf",
            )
            assert [r.get("event_type") for r in child_records] == [
                "run.accepted",
                "runner.selected",
                "run.dispatched",
                "run.completed",
            ]
            assert er_dispatch._delegated_calls == {}
    finally:
        writer.close()
        await _finish_run(ctx, project_dir, task_a, release_a)


async def test_child_error_reports_before_raise(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Reports sent before a child failure reach the root before it raises (G4)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(release_path=str(release_path), scenario="raise"),
        token,
    )
    try:
        await _wait_for_report(
            wm_client, token, "before-fail", collected, timeout=55.0, tasks=(task,)
        )
        try:
            await asyncio.wait_for(task, timeout=60.0)
        except Exception as exc:  # noqa: BLE001
            assert "leaf failed" in str(exc)
        else:
            raise AssertionError("expected the child error to fail the run")
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_child_exit_fails_parent(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A child runner dying without reply fails the parent run (row G8)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    token = f"root-{uuid.uuid4().hex}"
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(release_path=str(release_path), scenario="exit"),
        token,
    )
    try:
        try:
            await asyncio.wait_for(task, timeout=60.0)
        except (RuntimeError, Exception):  # noqa: BLE001
            pass
        else:
            raise AssertionError("expected the dead child to fail the run")
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_delegated_reports_keep_child_order(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Overlapping child scopes arrive at the root in child order (row G11)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    script = [
        {"op": "begin", "title": "scope-A"},
        {"op": "begin", "title": "scope-B"},
        {"op": "report", "message": "work"},
        {"op": "report"},
        {"op": "end"},
        {"op": "end"},
    ]
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(release_path=str(release_path), scenario="script", script=script),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        release_path.write_text("go\n", encoding="utf-8")
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        reports = [v.get("message") for v in collected if v.get("type") == "report"]
        assert (
            reports
            == ["probe leaf", "scope-A", "scope-B", "work"]
            + [
                "Delegated progress ended",
            ]
            * 3
        )
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_slow_pull_gets_bounded_tail(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A slow parent still gets the bounded tail ending with the end (G15/G16)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(
            release_path=str(release_path),
            scenario="flood",
            flood_count=1000,
            slow_pull_seconds=2.0,
        ),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        reports = [v for v in collected if v.get("type") == "report"]
        assert len(reports) <= 100
        assert reports[-1].get("message") == "Delegated progress ended"
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_big_message_relayed_intact(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A 1 MiB valid message is relayed intact (row G22)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(release_path=str(release_path), scenario="big"),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        big = [
            v
            for v in collected
            if v.get("type") == "report"
            and isinstance(v.get("message"), str)
            and len(v.get("message")) == 1024 * 1024
        ]
        assert len(big) == 1
        assert big[0]["message"] == "X" * (1024 * 1024)
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_raising_sink_drops_and_continues(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A failing parent sink drops that report but the run continues (row G19)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    script = [
        {"op": "report", "message": "lost"},
        {"op": "report", "message": "kept-a"},
        {"op": "report", "message": "kept-b"},
    ]
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(
            release_path=str(release_path),
            scenario="script",
            script=script,
            flaky_sink=True,
        ),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        reports = [v.get("message") for v in collected if v.get("type") == "report"]
        assert reports == ["kept-a", "kept-b", "Delegated progress ended"]
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_break_without_close_keeps_reports_until_aclose(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A bare break keeps reports flowing; the later close releases once (G11)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    async with _record_releases() as releases:
        release_path = project_dir / f"release-{uuid.uuid4().hex}"
        token = f"root-{uuid.uuid4().hex}"
        collected: list[dict] = []
        task = _start_root_run(
            wm_client,
            project_dir,
            _payload(
                release_path=str(release_path),
                streaming_partials=3,
                parent_close="break",
            ),
            token,
        )
        try:
            await _wait_for_report(
                wm_client, token, "stream-0", collected, timeout=55.0, tasks=(task,)
            )
            await asyncio.sleep(0.5)
            assert not releases, "a bare break must not release"
            result = await asyncio.wait_for(task, timeout=60.0)
            assert result["returnCode"] == 0
            assert len(releases) == 1
        finally:
            await _finish_run(ctx, project_dir, task, release_path)


async def test_late_progress_after_return_dropped(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Background sends racing past the return never reach the root (row G10)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(scenario="background"),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        await asyncio.sleep(2.0)
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        assert not any(
            isinstance(v.get("message"), str)
            and v.get("message", "").startswith("late-")
            for v in collected
            if v.get("type") == "report"
        )
    finally:
        await _finish_run(ctx, project_dir, task)


async def test_fanout_channels_share_parent_context(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Two concurrent calls on one context both deliver their reports (G13/G17)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_no_runtime")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    collected: list[dict] = []
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(
            release_path=str(release_path),
            message=f"fan-{uuid.uuid4().hex}",
            fanout=2,
        ),
        token,
    )
    try:
        result = await asyncio.wait_for(task, timeout=60.0)
        assert result["returnCode"] == 0
        await _collect_until_end(wm_client, token, collected, timeout=20.0)
        reports = [v.get("message") for v in collected if v.get("type") == "report"]
        assert any(m and not m.endswith("-b") for m in reports)
        assert any(m and m.endswith("-b") for m in reports)
    finally:
        await _finish_run(ctx, project_dir, task, release_path)


async def test_recursive_probe_refuses_at_depth_limit(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A 9-deep same-env delegation chain fails on the depth limit."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path, "dev_workspace")
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    token = f"root-{uuid.uuid4().hex}"
    task = _start_root_run(
        wm_client,
        project_dir,
        _payload(
            release_path=str(release_path),
            message=f"depth-{uuid.uuid4().hex}",
            delegations=9,
        ),
        token,
    )
    try:
        try:
            await asyncio.wait_for(task, timeout=60.0)
        except RuntimeError as exc:
            assert "Orchestration depth" in str(exc)
        else:
            raise AssertionError("expected depth-limit failure")
        assert in_flight_runs.runs_in_project(ctx, project_dir) == []
    finally:
        await _finish_run(ctx, project_dir, task, release_path)
