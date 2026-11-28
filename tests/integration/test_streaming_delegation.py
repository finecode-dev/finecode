"""Real WM + ER composition for delegated streaming runs.

Covers orchestration depth across the streaming delegation boundary, run
identity and in-flight tracking while a child is blocked, and WM WAL lifecycle
records for delegated streaming runs only.
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
from finecode.wm_server.services.run_service.er_dispatch import _BridgeHandlers

FIXTURE_MODULE = "streaming_delegation_probe"
RUN_AGENT_FIXTURE_MODULE = "streaming_run_agent_probe"


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _repo_run_agent_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{RUN_AGENT_FIXTURE_MODULE}.py"


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
    include_agent_probe: bool = False,
    project_name: str = "streaming-delegation-probe",
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
    if include_agent_probe:
        shutil.copy(
            _repo_run_agent_fixture_module(),
            fixture_modules / f"{RUN_AGENT_FIXTURE_MODULE}.py",
        )
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
            "\n".join(
                [
                    str(fixture_modules),
                    str(pathlib.Path(__file__).parents[2] / "extensions/fine_agent"),
                    str(pathlib.Path(__file__).parents[2] / "extensions/fine_agent_pi"),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    leaf = leaf_env
    agent_actions = ""
    if include_agent_probe:
        agent_actions = f"""

[tool.finecode.action.run_agent_task]
source = "fine_agent.run_agent_task_action.RunAgentTaskAction"
handlers = [{{ name = "router", source = "fine_agent.route_agent_task_handler.RouteAgentTaskHandler", env = "dev_workspace", config = {{ default_backend = "pi" }} }}]

[tool.finecode.action.run_pi_agent_task]
source = "fine_agent_pi.run_pi_agent_task_action.RunPiAgentTaskAction"
handlers = [{{ name = "fake_pi", source = "{RUN_AGENT_FIXTURE_MODULE}.FakePiHandler", env = "dev_workspace" }}]

[tool.finecode.action.close_routed_run]
source = "{RUN_AGENT_FIXTURE_MODULE}.CloseRoutedAction"
handlers = [{{ name = "close_router", source = "{RUN_AGENT_FIXTURE_MODULE}.CloseRoutedHandler", env = "dev_workspace" }}]
"""
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
handlers = [{{ name = "leaf", source = "{FIXTURE_MODULE}.ProbeLeafHandler", env = "{leaf}" }}]
{agent_actions}
""",
        encoding="utf-8",
    )
    return project_dir, fixture_modules


def _read_wal_records(wal_dir: pathlib.Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(wal_dir.glob("wal-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


@contextlib.asynccontextmanager
async def _record_releases() -> typing.AsyncGenerator[list[str | None], None]:
    calls: list[str | None] = []
    original = _BridgeHandlers.release_delegated_call

    async def record(
        self: typing.Any,
        runner: typing.Any,
        params: typing.Any,
        ws_context: typing.Any,
    ) -> typing.Any:
        calls.append(params.partial_result_token)
        return await original(self, runner, params, ws_context)

    _BridgeHandlers.release_delegated_call = record
    try:
        yield calls
    finally:
        _BridgeHandlers.release_delegated_call = original


async def _setup_agent_probe(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> pathlib.Path:
    project_dir, _ = _prepare_project(
        tmp_path, "dev_workspace", include_agent_probe=True
    )
    await wm_client.request(
        "client/initialize", {"clientId": f"agent-probe-{uuid.uuid4().hex}"}
    )
    added = await wm_client.request(
        "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
    )
    assert added is not None
    project = wm_client.ws_context.ws_projects.get(project_dir)
    assert project is not None
    assert project.status == domain.ProjectStatus.CONFIG_VALID
    runners = wm_client.ws_context.ws_projects_extension_runners.get(project_dir, {})
    assert runners, "real runners were not started"
    for runner in runners.values():
        assert runner.status.name == "RUNNING"
    return project_dir


def _agent_run_params(
    project_dir: pathlib.Path, prompt: str, *, token: str | None = None
) -> dict[str, typing.Any]:
    params: dict[str, typing.Any] = {
        "actionSource": "fine_agent.run_agent_task_action.RunAgentTaskAction",
        "project": str(project_dir),
        "params": {"prompt": prompt},
        "options": {"resultFormats": ["json"], "trigger": "user", "devEnv": "ci"},
    }
    if token is not None:
        params["partialResultToken"] = token
    return params


async def test_routed_agent_stream_reaches_mcp_before_final(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    project_dir = await _setup_agent_probe(wm_client, tmp_path)
    release_path = project_dir / "release-agent"
    token = f"agent-{uuid.uuid4().hex}"
    run = asyncio.create_task(
        wm_client.request(
            "actions/run",
            _agent_run_params(project_dir, f"wait:{release_path}", token=token),
            timeout=60.0,
        )
    )
    notifications: list[dict[str, typing.Any]] = []
    try:
        deadline = asyncio.get_running_loop().time() + 55.0
        latest = None
        while latest is None:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, "no running agent partial arrived"
            params = await wm_client.next_notification(
                "actions/partialResult", timeout=min(remaining, 5.0)
            )
            if params.get("token") != token:
                continue
            value = params.get("value", {})
            notifications.append(value)
            result = value.get("resultByFormat", {}).get("json", {})
            if result.get("status") == "running":
                assert result.get("status") != "failed"
                latest = result
        assert run is not None and not run.done()
        assert "café" in latest.get("output", "")
        assert latest["output"] == "café later"
        _ = release_path.write_text("go\n", encoding="utf-8")
        terminal = None
        while terminal is None:
            params = await wm_client.next_notification(
                "actions/partialResult", timeout=10.0
            )
            if params.get("token") != token:
                continue
            value = params.get("value", {})
            notifications.append(value)
            result = value.get("resultByFormat", {}).get("json", {})
            if result.get("status") == "settled":
                terminal = result
        response = await run
        assert response["returnCode"] == 0
        assert terminal is not None
        assert terminal["output"] == "canonical résumé"
        assert all(
            item.get("resultByFormat", {}).get("json", {}).get("status") != "failed"
            for item in notifications
        )
    finally:
        _ = release_path.write_text("go\n", encoding="utf-8")
        if not run.done():
            await asyncio.wait_for(run, timeout=60.0)


async def test_routed_agent_final_results_without_partial_token(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    project_dir = await _setup_agent_probe(wm_client, tmp_path)
    direct = await wm_client.request(
        "actions/run", _agent_run_params(project_dir, "plain"), timeout=60.0
    )
    assert direct["returnCode"] == 0
    assert direct["resultByFormat"]["json"]["output"] == "canonical résumé"

    batch = await wm_client.request(
        "actions/runBatch",
        {
            "actionSources": ["fine_agent.run_agent_task_action.RunAgentTaskAction"],
            "projects": [str(project_dir)],
            "params": {"prompt": "plain"},
            "options": {
                "resultFormats": ["json"],
                "mergeResults": True,
                "trigger": "user",
                "devEnv": "ci",
            },
        },
        timeout=60.0,
    )
    assert batch["returnCode"] == 0
    assert (
        batch["results"][str(project_dir)][
            "fine_agent.run_agent_task_action.RunAgentTaskAction"
        ]["resultByFormat"]["json"]["output"]
        == "canonical résumé"
    )


@pytest.mark.parametrize("tokenized", [False, True])
@pytest.mark.parametrize("prompt,profile", [("plain", "missing"), ("silent", None)])
async def test_routed_agent_failures_are_failed(
    wm_client: typing.Any,
    tmp_path: pathlib.Path,
    tokenized: bool,
    prompt: str,
    profile: str | None,
) -> None:
    project_dir = await _setup_agent_probe(wm_client, tmp_path)
    payload = {"prompt": prompt}
    if profile is not None:
        payload["profile"] = profile
    token = f"agent-failure-{uuid.uuid4().hex}" if tokenized else None
    params = _agent_run_params(project_dir, prompt, token=token)
    params["params"] = payload
    failed = None
    if token is None:
        response = await wm_client.request("actions/run", params, timeout=60.0)
    else:
        response = await wm_client.request("actions/run", params, timeout=60.0)
        deadline = asyncio.get_running_loop().time() + 5.0
        while failed is None:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, "failed partial did not arrive"
            partial = await wm_client.next_notification(
                "actions/partialResult", timeout=remaining
            )
            if partial.get("token") == token:
                candidate = (
                    partial.get("value", {}).get("resultByFormat", {}).get("json", {})
                )
                if candidate.get("status") == "failed":
                    failed = candidate
        assert failed is not None
    assert response["returnCode"] != 0
    if token is None:
        assert response["resultByFormat"]["json"]["status"] == "failed"
    else:
        assert failed is not None
        assert failed["status"] == "failed"


async def test_closing_router_releases_delegated_call(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    project_dir = await _setup_agent_probe(wm_client, tmp_path)
    release_path = project_dir / "release-close"
    async with _record_releases() as releases:
        result = await wm_client.request(
            "actions/run",
            {
                "actionSource": f"{RUN_AGENT_FIXTURE_MODULE}.CloseRoutedAction",
                "project": str(project_dir),
                "params": {"prompt": f"wait:{release_path}"},
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
            },
            timeout=60.0,
        )
    assert result["resultByFormat"]["json"]["output"] == "café later"
    assert releases


@pytest.mark.parametrize("leaf_env", ["dev_workspace", "dev_no_runtime"])
@pytest.mark.parametrize("wal_enabled", [False, True])
async def test_delegated_streaming_run_keeps_depth_identity_and_wal(
    wm_client: typing.Any,
    tmp_path: pathlib.Path,
    leaf_env: str,
    wal_enabled: bool,
) -> None:
    """Delegated streaming keeps depth, identity, tracking and WAL lifecycle."""
    project_dir, _ = _prepare_project(tmp_path, leaf_env)
    ctx = wm_client.ws_context
    writer: wal.WalWriter | None = None
    if wal_enabled:
        writer = wal.WalWriter(
            wal.WalConfig(enabled=True, dir_path=tmp_path / "wm-wal")
        )
        ctx.wal_writer = writer
    else:
        assert ctx.wal_writer is None

    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    root_task: asyncio.Task | None = None
    try:
        await wm_client.request(
            "client/initialize",
            {"clientId": "streaming-delegation-composition"},
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

        root_task = asyncio.ensure_future(
            wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ProbeParentAction",
                    "project": str(project_dir),
                    "params": {
                        "release_path": str(release_path),
                        "message": f"probe-{uuid.uuid4().hex}",
                        "delegations": 0,
                    },
                    "options": {
                        "resultFormats": ["json"],
                        "trigger": "user",
                        "devEnv": "ci",
                    },
                },
                timeout=60.0,
            )
        )
        deadline = asyncio.get_running_loop().time() + 55.0
        while True:
            runs = in_flight_runs.runs_in_project(ctx, project_dir)
            by_action = {run.action_name: run for run in runs}
            if "probe_leaf" in by_action:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("probe_leaf run did not appear in flight")
            await asyncio.sleep(0.05)
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
            assert writer is not None
            wal_dir = tmp_path / "wm-wal"
            child_records = [
                r
                for r in _read_wal_records(wal_dir)
                if r.get("wal_run_id") == leaf_run.run_id
            ]
            assert child_records, "no WAL records for child run"
            assert [r.get("event_type") for r in child_records] == [
                "run.accepted",
                "runner.selected",
                "run.dispatched",
                "run.completed",
            ]
            assert all(r.get("action_name") == "probe_leaf" for r in child_records)
            assert all(r.get("project_path") == str(project_dir) for r in child_records)
            dispatched = [
                r for r in child_records if r.get("event_type") == "run.dispatched"
            ]
            assert dispatched
            assert all(
                r.get("payload", {}).get("env_name") == leaf_env for r in dispatched
            )

            await wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ProbeLeafAction",
                    "project": str(project_dir),
                    "params": {
                        "release_path": str(release_path),
                        "message": f"direct-{uuid.uuid4().hex}",
                        "delegations": 0,
                    },
                    "partialResultToken": f"p-{uuid.uuid4().hex}",
                    "options": {
                        "resultFormats": ["json"],
                        "trigger": "user",
                        "devEnv": "ci",
                    },
                },
                timeout=60.0,
            )
            records = _read_wal_records(wal_dir)
            assert [
                r
                for r in records
                if r.get("action_name") == "probe_leaf"
                and r.get("wal_run_id") != leaf_run.run_id
            ] == []
        else:
            assert not list((tmp_path / "wm-wal").glob("wal-*.jsonl"))
    finally:
        try:
            if not release_path.exists():
                release_path.write_text("go\n", encoding="utf-8")
        except OSError:
            pass
        if root_task is not None and not root_task.done():
            root_task.cancel()
            try:
                await root_task
            except (asyncio.CancelledError, Exception):
                pass
        await shutdown_service.on_shutdown(ctx)
        if writer is not None:
            writer.close()
        ctx.wal_writer = None
        runners = ctx.ws_projects_extension_runners.get(project_dir, {})
        for _ in range(100):
            if not runners or all(r.status.name != "RUNNING" for r in runners.values()):
                break
            await asyncio.sleep(0.1)
        assert not runners or all(r.status.name != "RUNNING" for r in runners.values())


async def test_delegated_streaming_chain_refused_at_depth_limit(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A 9-deep streaming delegation chain fails on the depth limit."""
    project_dir, _ = _prepare_project(tmp_path, "dev_workspace")
    ctx = wm_client.ws_context
    ctx.wal_writer = None
    release_path = project_dir / f"release-{uuid.uuid4().hex}"
    release_path.write_text("go\n", encoding="utf-8")
    try:
        await wm_client.request(
            "client/initialize",
            {"clientId": "streaming-delegation-depth-limit"},
            timeout=60.0,
        )
        await wm_client.request(
            "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
        )
        try:
            await wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ProbeParentAction",
                    "project": str(project_dir),
                    "params": {
                        "release_path": str(release_path),
                        "message": f"depth-{uuid.uuid4().hex}",
                        "delegations": 9,
                    },
                    "options": {
                        "resultFormats": ["json"],
                        "trigger": "user",
                        "devEnv": "ci",
                    },
                },
                timeout=60.0,
            )
        except RuntimeError as exc:
            assert "Orchestration depth" in str(exc)
        else:
            raise AssertionError("expected depth-limit failure")
        assert in_flight_runs.runs_in_project(ctx, project_dir) == []
    finally:
        await shutdown_service.on_shutdown(ctx)
        ctx.wal_writer = None


def _leaf_request(
    release_by_project: dict[pathlib.Path, pathlib.Path], stream: str
) -> tuple[str, dict[str, typing.Any]]:
    def _payload(release_path: pathlib.Path) -> dict[str, typing.Any]:
        return {"release_path": str(release_path), "message": "m", "delegations": 0}

    options = {"resultFormats": ["json"], "trigger": "user", "devEnv": "ci"}
    token = f"t-{uuid.uuid4().hex}"
    if stream.startswith("batch"):
        token_key = (
            "progressToken" if stream == "batch_progress" else "partialResultToken"
        )
        first_release = next(iter(release_by_project.values()))
        return "actions/runBatch", {
            "actionSources": [f"{FIXTURE_MODULE}.ProbeLeafAction"],
            "projects": [str(project_dir) for project_dir in release_by_project],
            "params": _payload(first_release),
            "paramsByProject": {
                str(project_dir): _payload(release_path)
                for project_dir, release_path in release_by_project.items()
            },
            "options": options,
            token_key: token,
        }
    ((project_dir, release_path),) = release_by_project.items()
    return "actions/run", {
        "actionSource": f"{FIXTURE_MODULE}.ProbeLeafAction",
        "project": str(project_dir),
        "params": _payload(release_path),
        "options": options,
        "partialResultToken" if stream == "partial" else "progressToken": token,
    }


@pytest.mark.parametrize(
    ("stream", "project_count"),
    [
        ("partial", 1),
        ("progress", 1),
        ("batch", 1),
        ("batch_progress", 1),
        ("batch", 2),
    ],
)
async def test_client_disconnect_cancels_streamed_run_in_er(
    wm_client: typing.Any, tmp_path: pathlib.Path, stream: str, project_count: int
) -> None:
    """A client that disconnects during a streamed run gets every action handler
    it started in the ERs cancelled promptly, not run to its end (issues #998,
    #1003)."""
    from tests.integration.conftest import InProcClient

    release_by_project: dict[pathlib.Path, pathlib.Path] = {}
    for index in range(project_count):
        project_dir, _ = _prepare_project(
            tmp_path / f"p{index}",
            "dev_no_runtime",
            project_name=f"streaming-delegation-probe-{index}",
        )
        release_by_project[project_dir] = project_dir / f"release-{uuid.uuid4().hex}"
    cancelled_markers = [
        release_path.with_name(release_path.name + ".cancelled")
        for release_path in release_by_project.values()
    ]
    ctx = wm_client.ws_context
    run_task: asyncio.Future | None = None
    try:
        await wm_client.request(
            "client/initialize", {"clientId": "disconnect-observer"}, timeout=60.0
        )
        for project_dir in release_by_project:
            assert await wm_client.request(
                "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
            )
        reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
        runner_client = InProcClient(reader, writer, ctx)
        await runner_client.request(
            "client/initialize", {"clientId": "disconnecting-client"}, timeout=60.0
        )
        method, params = _leaf_request(release_by_project, stream)
        run_task = asyncio.ensure_future(
            runner_client.request(method, params, timeout=60.0)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 55.0
        while not all(
            any(
                run.action_name == "probe_leaf"
                for run in in_flight_runs.runs_in_project(ctx, project_dir)
            )
            for project_dir in release_by_project
        ):
            assert loop.time() < deadline, "probe_leaf runs did not appear in flight"
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # the handlers are now waiting in the ERs

        run_task.cancel()
        await runner_client.close()

        deadline = loop.time() + 1.0
        while (
            not all(marker.exists() for marker in cancelled_markers)
            and loop.time() < deadline
        ):
            await asyncio.sleep(0.02)
        not_cancelled = [
            str(marker.parent) for marker in cancelled_markers if not marker.exists()
        ]
        assert not not_cancelled, (
            "handlers not cancelled within 1 s of the client disconnecting: "
            f"{not_cancelled}"
        )
    finally:
        for release_path in release_by_project.values():
            try:
                release_path.write_text("go\n", encoding="utf-8")
            except OSError:
                pass
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)
