"""Integration: one shared project config dump across `create_envs` (#993).

Real in-process WM with real ER subprocesses from copied venvs (pattern of
`test_delegated_cancellation.py`). The `fine_envs` dispatcher prepares one
dump per project run and lends it to every `create_env` child; the fixture
`create_env` handler runs in another ER to prove the URI crosses the WM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import shutil
import subprocess
import sys
import sysconfig
import time
import typing
import uuid

import pytest

from finecode.wm_server import domain
from finecode.wm_server.services import shutdown_service
from tests.integration.test_delegated_cancellation import (
    _copy_env,
    _has_event,
    _read_events,
    _repair_copied_venv_path,
    _wait_for,
)

FIXTURE_MODULE = "shared_config_dump_probe"
B = 5.0


def _warning_text(project_dir: pathlib.Path) -> str:
    return (
        f"{project_dir}: a delegated call of this create_envs run may still be "
        "running, possibly a create_env handler still using the removed config dump"
    )


def _repo_fixture_module() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py"


def _handler(name: str, source: str, env: str) -> str:
    return f'{{ name = "{name}", source = "{source}", env = "{env}" }}'


def _prepare_probe_project(
    tmp_path: pathlib.Path,
    *,
    extra_envs: list[str] | None = None,
    actions_toml: str,
    project_name: str = "shared-config-dump-probe",
) -> pathlib.Path:
    for mod in ("finecode", "finecode_extension_runner", "finecode_extension_api"):
        try:
            __import__(mod)
        except ImportError as exc:
            raise RuntimeError(
                f"fixture setup failed: required module {mod!r} not importable "
                f"in {sys.executable}: {exc}"
            ) from exc
    leaf_envs = ["dev_no_runtime", *(extra_envs or [])]
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
    for leaf_env in leaf_envs:
        _copy_env(source_env, venvs_dir / leaf_env)
        _repair_copied_venv_path(venvs_dir / leaf_env, source_env)
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in {"dev_workspace", *leaf_envs}:
        purelib = venvs_dir / env_name / purelib_relative
        purelib.mkdir(parents=True, exist_ok=True)
        (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            str(fixture_modules) + "\n", encoding="utf-8"
        )
    groups = "\n".join(f'{env} = ["finecode_extension_runner"]' for env in leaf_envs)
    project_dir.joinpath("pyproject.toml").write_text(
        f"""[project]
name = "{project_name}"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
{groups}

{actions_toml}
""",
        encoding="utf-8",
    )
    return project_dir


def _create_envs_toml() -> str:
    return f"""[tool.finecode.action.create_envs]
source = "fine_envs.create_envs_action.CreateEnvsAction"
handlers_mode = "replace"
handlers = [{_handler("dispatch", "fine_envs.create_envs_dispatch_handler.CreateEnvsDispatchHandler", "dev_workspace")}]
"""


def _create_env_toml(handlers: list[tuple[str, str]]) -> str:
    specs = ", ".join(_handler(name, source, env) for name, (source, env) in handlers)
    return f"""[tool.finecode.action.create_env]
source = "fine_envs.create_env_action.CreateEnvAction"
handlers_mode = "replace"
handlers = [{specs}]
"""


def _dump_config_toml(marker_env: str) -> str:
    # Save runs before the marker: sequential cross-env segments do not share
    # run-context content, so the remote marker appends to the saved file.
    return f"""[tool.finecode.action.dump_config]
source = "fine_envs.dump_config_action.DumpConfigAction"
handlers_mode = "replace"
handlers = [{_handler("dump", "fine_envs.dump_config_handler.DumpConfigHandler", "dev_workspace")}, {_handler("save", "fine_envs.dump_config_save_handler.DumpConfigSaveHandler", "dev_workspace")}, {_handler("marker", f"{FIXTURE_MODULE}.ProbeDumpMarkerHandler", marker_env)}]
"""


def _leaf_toml() -> str:
    return f"""[tool.finecode.action.probe_leaf]
source = "{FIXTURE_MODULE}.ProbeStubbornLeafAction"
handlers_mode = "replace"
handlers = [{_handler("leaf", f"{FIXTURE_MODULE}.ProbeStubbornLeafHandler", "dev_no_runtime_2")}]
"""


def _env_dict(
    project_dir: pathlib.Path, name: str, interpreter: str | None = None
) -> dict[str, typing.Any]:
    return {
        "name": name,
        "venv_dir_path": (project_dir / ".venvs" / name).as_uri(),
        "project_def_path": (project_dir / "pyproject.toml").as_uri(),
        "dependencies_override": [],
        "interpreter": interpreter,
    }


async def _setup(
    wm_client: typing.Any,
    tmp_path: pathlib.Path,
    *,
    actions_toml: str,
    extra_envs: list[str] | None = None,
    config: dict[str, typing.Any] | None = None,
    project_name: str = "shared-config-dump-probe",
) -> tuple[pathlib.Path, pathlib.Path]:
    project_dir = _prepare_probe_project(
        tmp_path,
        extra_envs=extra_envs,
        actions_toml=actions_toml,
        project_name=project_name,
    )
    await wm_client.request(
        "client/initialize", {"clientId": f"shared-dump-{uuid.uuid4().hex}"}
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
    full_config = {"log_path": str(log_path), **(config or {})}
    (project_dir / "probe-config.json").write_text(
        json.dumps(full_config), encoding="utf-8"
    )
    return project_dir, log_path


async def _run_create_envs(
    wm_client: typing.Any,
    project_dir: pathlib.Path,
    envs: list[dict[str, typing.Any]],
    *,
    timeout: float = 60.0,
) -> dict[str, typing.Any]:
    result = await wm_client.request(
        "actions/run",
        {
            "actionSource": "fine_envs.create_envs_action.CreateEnvsAction",
            "project": str(project_dir),
            "params": {"envs": envs},
            "options": {
                "resultFormats": ["json"],
                "trigger": "user",
                "devEnv": "ci",
            },
        },
        timeout=timeout,
    )
    assert isinstance(result, dict)
    return result


async def _subscribe_warnings(wm_client: typing.Any) -> None:
    await wm_client.request("server/subscribeLogs", {"minLevel": "WARNING"})


async def _drain_warnings(wm_client: typing.Any, duration: float) -> list[str]:
    messages: list[str] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        try:
            params = await wm_client.next_notification(
                "server/logRecords", timeout=max(0.1, deadline - time.monotonic())
            )
        except TimeoutError:
            break
        for record in params.get("records", []):
            if record.get("level") == "WARNING":
                messages.append(record.get("message", ""))
    return messages


def _uri_dirs(events: list[dict], what: str) -> list[pathlib.Path]:
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    dirs = []
    for event in events:
        if event.get("what") == what and event.get("uri"):
            parsed = urlparse(event["uri"])
            assert parsed.scheme == "file"
            dirs.append(pathlib.Path(url2pathname(parsed.path)).parent.parent)
    return dirs


def _uri_file(uri: str) -> pathlib.Path:
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    return pathlib.Path(url2pathname(parsed.path))


async def _start_run(
    wm_client: typing.Any, project_dir: pathlib.Path, envs: list[dict[str, typing.Any]]
) -> tuple[typing.Any, asyncio.Future]:
    from tests.integration.conftest import InProcClient

    ctx = wm_client.ws_context
    reader, writer = await asyncio.open_connection("127.0.0.1", wm_client.port)
    runner_client = InProcClient(reader, writer, ctx)
    await runner_client.request(
        "client/initialize",
        {"clientId": f"shared-dump-run-{uuid.uuid4().hex}"},
        timeout=60.0,
    )
    task = asyncio.ensure_future(
        runner_client.request(
            "actions/run",
            {
                "actionSource": "fine_envs.create_envs_action.CreateEnvsAction",
                "project": str(project_dir),
                "params": {"envs": envs},
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
            },
            timeout=60.0,
        )
    )
    return runner_client, task


async def test_shared_dump_reaches_remote_children(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Both remote children receive the identical dump-file URI and read
    the marker from that file itself; the file's directory is gone after
    the run with no warning."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
    )
    ctx = wm_client.ws_context
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a"), _env_dict(project_dir, "env-b")]
        result = await _run_create_envs(wm_client, project_dir, envs)
        assert result["returnCode"] == 0
        events = _read_events(log_path)
        seen = [e for e in events if e.get("what") == "saw-uri"]
        assert len(seen) == 2
        uris = [e["uri"] for e in seen]
        assert uris[0] is not None and uris[0] == uris[1]
        assert uris[0].startswith("file://")
        lent = _uri_file(uris[0])
        assert lent.name == "pyproject.toml"
        assert lent.parent.name == project_dir.name
        assert lent.parent.parent.name.startswith("finecode_config_dump_")
        markers = [e for e in events if e.get("what") == "marker"]
        assert len(markers) == 2
        assert all(e.get("marker") is True for e in markers)
        assert all(e.get("is_file") is True for e in markers)
        assert all(e.get("file_name") == "pyproject.toml" for e in markers)
        dirs = _uri_dirs(events, "saw-uri")
        assert len(dirs) == 2
        assert all(not d.exists() for d in dirs)
        warnings = await _drain_warnings(wm_client, 4.0)
        assert _warning_text(project_dir) not in warnings
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_real_uv_creations_share_one_dump(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Two real venvs are created from the shared dump: no errors, valid
    venvs, and the shared directory is gone afterwards."""
    source_bin = pathlib.Path(sys.executable).parent
    if not (source_bin / "uv").exists() and shutil.which("uv") is None:
        pytest.skip("uv binary not available in this environment")
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "uv",
                            (
                                "fine_python_uv.create_env_handler.UvCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
    )
    ctx = wm_client.ws_context
    try:
        interpreter = f"cpython@{sys.version_info.major}.{sys.version_info.minor}"
        envs = [
            _env_dict(project_dir, "env-a", interpreter),
            _env_dict(project_dir, "env-b", interpreter),
        ]
        result = await _run_create_envs(wm_client, project_dir, envs, timeout=180.0)
        assert result["returnCode"] == 0, result
        for env in envs:
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            venv_dir = pathlib.Path(url2pathname(urlparse(env["venv_dir_path"]).path))
            assert (venv_dir / "pyvenv.cfg").is_file()
            candidates = [
                venv_dir / "bin" / "python",
                venv_dir / "Scripts" / "python.exe",
            ]
            venv_python = next(p for p in candidates if p.exists())
            proc = subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)",
                ],
                capture_output=True,
                timeout=60,
            )
            assert proc.returncode == 0
        events = _read_events(log_path)
        dumped = [e for e in events if e.get("what") == "dumped"]
        assert dumped, "marker handler never recorded the dump directory"
        for event in dumped:
            assert not pathlib.Path(event["dir"]).parent.exists()
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_cooperative_cancel_stops_children_without_warning(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """Cancelling the top-level run stops both cooperative children within
    the bound, removes the directory, and logs no unconfirmed warning."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
        config={"mode": "cooperative", "release": ""},
    )
    ctx = wm_client.ws_context
    runner_client: typing.Any = None
    run_task: asyncio.Future | None = None
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a"), _env_dict(project_dir, "env-b")]
        runner_client, run_task = await _start_run(wm_client, project_dir, envs)
        await _wait_for(
            lambda: (
                sum(1 for e in _read_events(log_path) if e.get("what") == "started")
                >= 2
            ),
            timeout=55.0,
        )
        cancel_at = time.time()
        run_task.cancel()
        await runner_client.close()
        runner_client = None
        await _wait_for(
            lambda: (
                sum(1 for e in _read_events(log_path) if e.get("what") == "cancel_seen")
                >= 2
            ),
            timeout=10.0,
        )
        await _wait_for(
            lambda: (
                sum(1 for e in _read_events(log_path) if e.get("what") == "cleanup_end")
                >= 2
            ),
            timeout=B,
        )
        events = _read_events(log_path)
        for event in events:
            if event.get("what") == "cleanup_end":
                assert event["t"] - cancel_at < B
        dirs = _uri_dirs(events, "started")
        assert dirs
        await _wait_for(lambda: all(not d.exists() for d in dirs), timeout=B)
        warnings = await _drain_warnings(wm_client, 2.0)
        assert _warning_text(project_dir) not in warnings
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_sibling_failure_fails_run_and_removes_dump(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """One failing remote child fails the run with its message while the
    cooperative sibling is cancelled; the directory is gone, no warning."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
        config={"mode": "cooperative", "release": "", "raise_envs": ["env-a"]},
    )
    ctx = wm_client.ws_context
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a"), _env_dict(project_dir, "env-b")]
        with pytest.raises(Exception, match="probe-child-boom"):
            await _run_create_envs(wm_client, project_dir, envs)
        events = _read_events(log_path)
        assert _has_event(events, "env-b", "cancel_seen")
        dirs = _uri_dirs(events, "started")
        assert dirs
        assert all(not d.exists() for d in dirs)
        warnings = await _drain_warnings(wm_client, 2.0)
        assert _warning_text(project_dir) not in warnings
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_stubborn_child_warns_once_and_removes_dump(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A child ignoring cancellation makes the run end unconfirmed: exactly
    one warning names the project and the directory is gone in bound."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
        config={"mode": "stubborn"},
    )
    ctx = wm_client.ws_context
    runner_client: typing.Any = None
    run_task: asyncio.Future | None = None
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a")]
        runner_client, run_task = await _start_run(wm_client, project_dir, envs)
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "env-a", "started"),
            timeout=55.0,
        )
        cancel_at = time.time()
        run_task.cancel()
        await runner_client.close()
        runner_client = None
        deadline = cancel_at + B + 6.0 + 2.0
        warned: list[str] = []
        gone = False
        while time.time() < deadline:
            params = None
            try:
                params = await wm_client.next_notification(
                    "server/logRecords",
                    timeout=max(0.1, deadline - time.time()),
                )
            except TimeoutError:
                break
            for record in (params or {}).get("records", []):
                if record.get("level") == "WARNING":
                    warned.append(record.get("message", ""))
            events = _read_events(log_path)
            dirs = _uri_dirs(events, "started")
            if dirs and all(not d.exists() for d in dirs):
                gone = True
            if gone and warned.count(_warning_text(project_dir)) >= 1:
                break
        assert gone, "shared directory was not removed after the bound"
        assert warned.count(_warning_text(project_dir)) == 1, warned
        assert time.time() - cancel_at < B + 6.0 + 2.0
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_nested_unconfirmed_child_warns_once(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A child returning normally after its own nested call ended
    unconfirmed makes the run succeed with exactly one warning."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
                _leaf_toml(),
            ]
        ),
        extra_envs=["dev_no_runtime_2"],
        config={"mode": "nested"},
    )
    ctx = wm_client.ws_context
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a")]
        result = await _run_create_envs(wm_client, project_dir, envs, timeout=120.0)
        assert result["returnCode"] == 0, result
        events = _read_events(log_path)
        assert _has_event(events, "env-a", "nested_cancelled")
        dirs = _uri_dirs(events, "started")
        assert dirs
        assert all(not d.exists() for d in dirs)
        warnings = await _drain_warnings(wm_client, 4.0)
        assert warnings.count(_warning_text(project_dir)) == 1, warnings
    finally:
        await shutdown_service.on_shutdown(ctx)


async def test_mixed_env_handlers_warn_once(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A WM-routed call with a stubborn segment in the dispatcher's own ER
    ends unconfirmed on cancel: one warning, directory gone."""
    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "stubborn",
                            (
                                f"{FIXTURE_MODULE}.ProbeStubbornCreateEnvHandler",
                                "dev_workspace",
                            ),
                        ),
                        (
                            "cooperative",
                            (
                                f"{FIXTURE_MODULE}.ProbeCooperativeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        ),
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
    )
    ctx = wm_client.ws_context
    runner_client: typing.Any = None
    run_task: asyncio.Future | None = None
    try:
        await _subscribe_warnings(wm_client)
        envs = [_env_dict(project_dir, "env-a")]
        runner_client, run_task = await _start_run(wm_client, project_dir, envs)
        # Segments run sequentially: the stubborn segment in the dispatcher's
        # own ER starts first and never yields until cancellation.
        await _wait_for(
            lambda: _has_event(_read_events(log_path), "env-a", "started"),
            timeout=55.0,
        )
        cancel_at = time.time()
        run_task.cancel()
        await runner_client.close()
        runner_client = None
        deadline = cancel_at + B + 8.0
        warned: list[str] = []
        gone = False
        while time.time() < deadline:
            try:
                params = await wm_client.next_notification(
                    "server/logRecords",
                    timeout=max(0.1, deadline - time.time()),
                )
            except TimeoutError:
                break
            for record in (params or {}).get("records", []):
                if record.get("level") == "WARNING":
                    warned.append(record.get("message", ""))
            events = _read_events(log_path)
            dumped = [e for e in events if e.get("what") == "dumped"]
            dirs = [pathlib.Path(e["dir"]).parent for e in dumped if e.get("dir")]
            if dirs and all(not d.exists() for d in dirs):
                gone = True
            if gone and warned.count(_warning_text(project_dir)) >= 1:
                break
        assert gone, "shared directory was not removed after the bound"
        assert warned.count(_warning_text(project_dir)) == 1, warned
    finally:
        if runner_client is not None:
            with contextlib.suppress(Exception):
                await runner_client.close()
        if run_task is not None and not run_task.done():
            run_task.cancel()
        await shutdown_service.on_shutdown(ctx)


async def test_unknown_project_env_fails_with_project_error(
    wm_client: typing.Any, tmp_path: pathlib.Path
) -> None:
    """A batch with an unknown project's env fails naming that project: no child
    starts, no run directory is added, and no warning is logged."""
    import tempfile

    project_dir, log_path = await _setup(
        wm_client,
        tmp_path,
        actions_toml="\n".join(
            [
                _create_envs_toml(),
                _create_env_toml(
                    [
                        (
                            "probe",
                            (
                                f"{FIXTURE_MODULE}.ProbeCreateEnvHandler",
                                "dev_no_runtime",
                            ),
                        )
                    ]
                ),
                _dump_config_toml("dev_no_runtime"),
            ]
        ),
    )
    ctx = wm_client.ws_context
    try:
        other_def = tmp_path / "other" / "pyproject.toml"
        other_def.parent.mkdir(parents=True, exist_ok=True)
        other_def.write_text("[project]\n", encoding="utf-8")
        before = {
            p.name
            for p in pathlib.Path(tempfile.gettempdir()).iterdir()
            if p.name.startswith("finecode_config_dump_")
        }
        envs = [
            _env_dict(project_dir, "env-here"),
            {
                "name": "env-there",
                "venv_dir_path": (tmp_path / "venvs" / "env-there").as_uri(),
                "project_def_path": other_def.as_uri(),
                "dependencies_override": [],
                "interpreter": None,
            },
        ]
        with pytest.raises(RuntimeError) as exc_info:
            await _run_create_envs(wm_client, project_dir, envs)
        text = str(exc_info.value)
        assert "Failed to prepare the config dump of" in text
        assert str(other_def) in text
        events = _read_events(log_path)
        assert not [e for e in events if e.get("what") == "saw-uri"]
        after = {
            p.name
            for p in pathlib.Path(tempfile.gettempdir()).iterdir()
            if p.name.startswith("finecode_config_dump_")
        }
        assert after == before
        warnings = await _drain_warnings(wm_client, 2.0)
        assert _warning_text(project_dir) not in warnings
    finally:
        await shutdown_service.on_shutdown(ctx)
