"""Real WM + ER composition for delegated streaming runs.

Covers orchestration depth across the streaming delegation boundary, run
identity and in-flight tracking while a child is blocked, and WM WAL lifecycle
records for delegated streaming runs only.
"""

from __future__ import annotations

import asyncio
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

FIXTURE_MODULE = "streaming_delegation_probe"


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
name = "streaming-delegation-probe"
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
