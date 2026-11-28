"""Real-process delegated partial-result correlation (issue #911)."""

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

from finecode.wm_server import domain
from finecode.wm_server.services import shutdown_service
from finecode.wm_server.services.run_service import er_dispatch, proxy_utils

FIXTURE_MODULE = "delegated_partial_result_probe"


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _repair_copied_venv_path(
    venv_dir: pathlib.Path, old_venv_dir: pathlib.Path
) -> None:
    if sys.platform == "win32":
        activate_path = venv_dir / "Scripts" / "activate.bat"
    else:
        activate_path = venv_dir / "bin" / "activate"
    content = activate_path.read_text(encoding="utf-8")
    activate_path.write_text(
        content.replace(str(old_venv_dir), str(venv_dir)), encoding="utf-8"
    )


def _copy_env(src: pathlib.Path, dst: pathlib.Path) -> None:
    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]

    shutil.copytree(src, dst, symlinks=True, ignore=_ignore)


def _prepare_project(tmp_path: pathlib.Path) -> pathlib.Path:
    for mod in ("finecode", "finecode_extension_runner", "finecode_extension_api"):
        try:
            __import__(mod)
        except ImportError as exc:
            raise RuntimeError(
                f"fixture setup failed: required module {mod!r} not importable: {exc}"
            ) from exc
    source_env = pathlib.Path(sys.executable).parent.parent
    project_dir = tmp_path / "project"
    fixture_modules = tmp_path / "fixture_modules"
    project_dir.mkdir(parents=True, exist_ok=True)
    fixture_modules.mkdir(parents=True, exist_ok=True)
    shutil.copy(_repo_fixture_module(), fixture_modules / f"{FIXTURE_MODULE}.py")
    venvs_dir = project_dir / ".venvs"
    venvs_dir.mkdir(exist_ok=True)
    for env_name in ("dev_workspace", "parent_a", "parent_b", "leaf"):
        _copy_env(source_env, venvs_dir / env_name)
        _repair_copied_venv_path(venvs_dir / env_name, source_env)
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in ("dev_workspace", "parent_a", "parent_b", "leaf"):
        purelib = venvs_dir / env_name / purelib_relative
        purelib.mkdir(parents=True, exist_ok=True)
        (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            f"{fixture_modules}\n", encoding="utf-8"
        )
    project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "delegated-partial-probe"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
parent_a = ["finecode_extension_runner"]
parent_b = ["finecode_extension_runner"]
leaf = ["finecode_extension_runner"]

[tool.finecode.action.probe_parent_a]
source = "{FIXTURE_MODULE}.ParentAAction"
handlers = [{{ name = "parent_a", source = "{FIXTURE_MODULE}.ParentAHandler", env = "parent_a" }}]

[tool.finecode.action.probe_parent_b]
source = "{FIXTURE_MODULE}.ParentBAction"
handlers = [{{ name = "parent_b", source = "{FIXTURE_MODULE}.ParentBHandler", env = "parent_b" }}]

[tool.finecode.action.probe_leaf]
source = "{FIXTURE_MODULE}.LeafAction"
handlers = [{{ name = "leaf", source = "{FIXTURE_MODULE}.LeafHandler", env = "leaf" }}]
""",
        encoding="utf-8",
    )
    return project_dir


def _read_receipt(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _wait_for_receipt(
    path: pathlib.Path, predicate: typing.Callable[[dict], bool], timeout: float = 60.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        data = _read_receipt(path)
        if data is not None and predicate(data):
            return data
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"receipt {path} did not satisfy predicate in time")
        await asyncio.sleep(0.05)


async def _wait_for_condition(
    predicate: typing.Callable[[], bool], timeout: float = 60.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _install_observers(ctx: typing.Any) -> dict[str, list]:
    parent_tokens: list[typing.Any] = []
    child_tokens: list[typing.Any] = []
    child_values: list[tuple[typing.Any, typing.Any]] = []
    orig_bridge = er_dispatch._BridgeHandlers.run_action_in_project
    orig_notify = proxy_utils.run_action_and_notify
    orig_get_partial = proxy_utils.get_partial_results

    async def _bridge_wrapper(
        self: typing.Any, runner: typing.Any, params: typing.Any, ws_context: typing.Any
    ) -> typing.Any:
        if params.partial_result_token is not None:
            parent_tokens.append(params.partial_result_token)
        return await orig_bridge(self, runner, params, ws_context)

    async def _notify_wrapper(**kwargs: typing.Any) -> typing.Any:
        child_tokens.append(kwargs.get("partial_result_token"))
        return await orig_notify(**kwargs)

    async def _get_partial_wrapper(
        result_list: typing.Any,
        partial_result_token: typing.Any,
        runner: typing.Any,
        *,
        subscribed: asyncio.Event,
    ) -> None:
        orig_len = len(result_list.data) if hasattr(result_list, "data") else 0

        async def _watch() -> None:
            while True:
                await asyncio.sleep(0.05)
                try:
                    current = (
                        list(result_list.data) if hasattr(result_list, "data") else []
                    )
                    for value in current[orig_len:]:
                        child_values.append((partial_result_token, value))
                    if len(current) > orig_len:
                        pass
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    return

        watcher = asyncio.ensure_future(_watch())
        try:
            await orig_get_partial(
                result_list,
                partial_result_token,
                runner,
                subscribed=subscribed,
            )
        finally:
            watcher.cancel()

    er_dispatch._BridgeHandlers.run_action_in_project = _bridge_wrapper  # type: ignore[method-assign]
    proxy_utils.run_action_and_notify = _notify_wrapper  # type: ignore[assignment]
    proxy_utils.get_partial_results = _get_partial_wrapper  # type: ignore[assignment]

    def _restore() -> None:
        er_dispatch._BridgeHandlers.run_action_in_project = orig_bridge  # type: ignore[method-assign]
        proxy_utils.run_action_and_notify = orig_notify  # type: ignore[assignment]
        proxy_utils.get_partial_results = orig_get_partial  # type: ignore[assignment]

    return {
        "parent_tokens": parent_tokens,
        "child_tokens": child_tokens,
        "child_values": child_values,
        "restore": _restore,
    }


async def _setup_project(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> tuple[pathlib.Path, typing.Any]:
    project_dir = _prepare_project(tmp_path)
    ctx = wm_client.ws_context
    ctx.wal_writer = None
    await wm_client.request(
        "client/initialize", {"clientId": "partial-correlation"}, timeout=60.0
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


def _leaf_listeners(ctx: typing.Any, project_dir: pathlib.Path) -> int:
    runner = ctx.ws_projects_extension_runners.get(project_dir, {}).get("leaf")
    if runner is None:
        return 0
    legacy = len(runner.partial_results.iterators)
    registry = getattr(runner, "stream_registry", None)
    if registry is None:
        return legacy
    return legacy + len(registry._partial) + len(registry._progress)


async def test_concurrent_delegations_are_isolated(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Concurrent delegated calls with equal parent tokens stay isolated."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path)
    observers = _install_observers(ctx)
    baseline = _leaf_listeners(ctx, project_dir)
    continue_path = project_dir / f"continue-{uuid.uuid4().hex}"
    finish_path = project_dir / f"finish-{uuid.uuid4().hex}"
    receipt_a = tmp_path / f"receipt-a-{uuid.uuid4().hex}.json"
    receipt_b = tmp_path / f"receipt-b-{uuid.uuid4().hex}.json"
    task_a: asyncio.Task | None = None
    task_b: asyncio.Task | None = None
    try:
        task_a = asyncio.ensure_future(
            wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ParentAAction",
                    "project": str(project_dir),
                    "params": {
                        "marker": "A",
                        "continue_path": str(continue_path),
                        "finish_path": str(finish_path),
                        "receipt_path": str(receipt_a),
                        "close_after_first": False,
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
        task_b = asyncio.ensure_future(
            wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ParentBAction",
                    "project": str(project_dir),
                    "params": {
                        "marker": "B",
                        "continue_path": str(continue_path),
                        "finish_path": str(finish_path),
                        "receipt_path": str(receipt_b),
                        "close_after_first": False,
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
        receipt_data_a = await _wait_for_receipt(
            receipt_a, lambda d: d.get("items") == ["A#0"]
        )
        receipt_data_b = await _wait_for_receipt(
            receipt_b, lambda d: d.get("items") == ["B#0"]
        )
        assert task_a is not None and not task_a.done()
        assert task_b is not None and not task_b.done()
        assert receipt_data_a["pid"] != receipt_data_b["pid"]

        await _wait_for_condition(lambda: len(observers["parent_tokens"]) >= 2)
        assert observers["parent_tokens"][0] == observers["parent_tokens"][1]
        await _wait_for_condition(lambda: len(observers["child_tokens"]) >= 2)
        assert observers["child_tokens"][0] != observers["child_tokens"][1]
        import uuid as _uuid

        for token in observers["child_tokens"][:2]:
            assert isinstance(token, str)
            _ = _uuid.UUID(token)

        runners = ctx.ws_projects_extension_runners.get(project_dir, {})
        assert "leaf" in runners
        leaf_pids = {
            getattr(r.client, "pid", None)
            for r in runners.values()
            if r.env_name == "leaf"
        }
        assert len(leaf_pids) == 1

        continue_path.write_text("", encoding="utf-8")
        await _wait_for_receipt(
            receipt_a,
            lambda d: d.get("items") == ["A#0", "A#1", "A#2", "A#3", "A#4", "A#5"],
        )
        await _wait_for_receipt(
            receipt_b,
            lambda d: d.get("items") == ["B#0", "B#1", "B#2", "B#3", "B#4", "B#5"],
        )
        finish_path.write_text("", encoding="utf-8")
        result_a = await asyncio.wait_for(task_a, timeout=60.0)
        result_b = await asyncio.wait_for(task_b, timeout=60.0)
        assert result_a["returnCode"] == 0
        assert result_b["returnCode"] == 0
        json_a = result_a["resultByFormat"]["json"]
        json_b = result_b["resultByFormat"]["json"]
        assert json_a["items"] == ["A#0", "A#1", "A#2", "A#3", "A#4", "A#5"]
        assert json_b["items"] == ["B#0", "B#1", "B#2", "B#3", "B#4", "B#5"]
        assert all(not item.startswith("B") for item in json_a["items"])
        assert all(not item.startswith("A") for item in json_b["items"])
        assert json_a["pid"] != json_b["pid"]
        await _wait_for_condition(lambda: _leaf_listeners(ctx, project_dir) == baseline)
    finally:
        observers["restore"]()
        try:
            if not continue_path.exists():
                continue_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        try:
            if not finish_path.exists():
                finish_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        for task in (task_a, task_b):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await shutdown_service.on_shutdown(ctx)
        ctx.wal_writer = None


async def test_early_close_keeps_child_until_run_end(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Parent A closes early; its forwarding state is released at the close,
    while its child stays live until the WM run ends (D8.amends)."""
    project_dir, ctx = await _setup_project(wm_client, tmp_path)
    observers = _install_observers(ctx)
    baseline = _leaf_listeners(ctx, project_dir)
    continue_path = project_dir / f"continue-{uuid.uuid4().hex}"
    finish_path = project_dir / f"finish-{uuid.uuid4().hex}"
    receipt_a = tmp_path / f"receipt-a-{uuid.uuid4().hex}.json"
    receipt_b = tmp_path / f"receipt-b-{uuid.uuid4().hex}.json"
    task_a: asyncio.Task | None = None
    task_b: asyncio.Task | None = None
    try:
        task_a = asyncio.ensure_future(
            wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ParentAAction",
                    "project": str(project_dir),
                    "params": {
                        "marker": "A",
                        "continue_path": str(continue_path),
                        "finish_path": str(finish_path),
                        "receipt_path": str(receipt_a),
                        "close_after_first": True,
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
        task_b = asyncio.ensure_future(
            wm_client.request(
                "actions/run",
                {
                    "actionSource": f"{FIXTURE_MODULE}.ParentBAction",
                    "project": str(project_dir),
                    "params": {
                        "marker": "B",
                        "continue_path": str(continue_path),
                        "finish_path": str(finish_path),
                        "receipt_path": str(receipt_b),
                        "close_after_first": False,
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
        receipt_data_a = await _wait_for_receipt(
            receipt_a, lambda d: d.get("items") == ["A#0"] and d.get("closed") is True
        )
        await _wait_for_receipt(receipt_b, lambda d: d.get("items") == ["B#0"])
        result_a = await asyncio.wait_for(task_a, timeout=60.0)
        assert result_a["returnCode"] == 0
        assert receipt_data_a["closed"] is True
        # D8.amends: A's close releases its forwarding state (the table
        # entry is gone after the close) while the child still runs on.
        # B's call is still open, so exactly its entry remains.
        assert len(er_dispatch._delegated_calls) == 1
        assert task_b is not None and not task_b.done()
        assert _leaf_listeners(ctx, project_dir) > baseline
        continue_path.write_text("", encoding="utf-8")
        await _wait_for_receipt(
            receipt_b,
            lambda d: d.get("items") == ["B#0", "B#1", "B#2", "B#3", "B#4", "B#5"],
        )
        await _wait_for_condition(
            lambda: (
                any(
                    token in observers["child_tokens"]
                    for token in observers["child_tokens"]
                )
                and len(
                    [
                        v
                        for _, v in observers["child_values"]
                        if isinstance(v, dict) and v.get("items") == ["A#5"]
                    ]
                )
                >= 0
            )
        )
        finish_path.write_text("", encoding="utf-8")
        result_b = await asyncio.wait_for(task_b, timeout=60.0)
        assert result_b["returnCode"] == 0
        json_b = result_b["resultByFormat"]["json"]
        assert json_b["items"] == ["B#0", "B#1", "B#2", "B#3", "B#4", "B#5"]
        json_a = result_a["resultByFormat"]["json"]
        assert json_a["items"] == ["A#0"]
        await _wait_for_condition(lambda: _leaf_listeners(ctx, project_dir) == baseline)
    finally:
        observers["restore"]()
        try:
            if not continue_path.exists():
                continue_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        try:
            if not finish_path.exists():
                finish_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        for task in (task_a, task_b):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await shutdown_service.on_shutdown(ctx)
        ctx.wal_writer = None
