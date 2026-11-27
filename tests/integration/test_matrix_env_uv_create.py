"""Real-uv matrix env creation: resolved config stays PEP 735-valid.

Every child is created through `UvCreateEnvHandler` with an explicit
`--python`, against a config dumped by `render_config_dump`. A `Failed to
parse` in uv's stderr means the dump still carries an invalid group name.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any, cast

import pytest
from fine_envs import create_env_action, dump_config_action
from fine_envs.create_envs_action import EnvInfo
from fine_envs.dump_config_render import render_config_dump
from fine_envs.project_config_dump import write_project_config_dump
from fine_python_uv._uv_common import get_uv_executable
from fine_python_uv.create_env_handler import UvCreateEnvHandler
from finecode_extension_api.interfaces import (
    icommandrunner,
    ifilemanager,
    ilogger,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner.impls.command_runner import (
    CommandRunner,
    CommandRunnerConfig,
)
from finecode_extension_runner.process_slots import ProcessSlots
from finecode_extension_runner.testing import NoOpLogger, run_handler

from finecode.wm_server import context, domain
from finecode.wm_server.config.read_configs import read_project_config
from finecode_extension_api import code_action

pytestmark = pytest.mark.skipif(
    not get_uv_executable().exists(),
    reason="uv binary not available in this environment",
)


def _write_pyproject(
    tmp_path: pathlib.Path, requires_python: str, interpreters: list[str]
) -> pathlib.Path:
    interp_list = ", ".join(f'"{i}"' for i in interpreters)
    content = (
        "[project]\n"
        'name = "sample"\n'
        'version = "0.1.0"\n'
        f'requires-python = "{requires_python}"\n'
        "\n"
        "[dependency-groups]\n"
        'testing = ["pytest"]\n'
        'dev = ["pytest"]\n'
        "\n"
        "[tool.finecode.env.testing]\n"
        f"interpreters = [{interp_list}]\n"
        "\n"
        "[tool.finecode.env.dev]\n"
        "install_project = false\n"
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    return path


def _resolved_config(tmp_path: pathlib.Path) -> dict[str, Any]:
    project = domain.Project(
        name="sample",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    read_project_config(project, ws_context)
    return dict(ws_context.ws_projects_raw_configs[tmp_path])


class _DumpActionRunner:
    """Write `render_config_dump(config, {})` to the payload's target."""

    async def get_actions_for_parent(self, parent_action_type: type) -> dict[str, Any]:
        raise NotImplementedError

    async def run_action(
        self, action_type: Any, payload: Any, meta: Any, caller_kwargs: Any = None
    ) -> None:
        assert isinstance(payload, dump_config_action.DumpConfigRunPayload)
        target = resource_uri_to_path(payload.target_file_path)
        target.write_text(
            render_config_dump(payload.project_raw_config, {}), encoding="utf-8"
        )

    def run_action_iter(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _ProjectInfoStub:
    def __init__(self, project_dir: pathlib.Path, raw_config: dict[str, Any]) -> None:
        self._dir = project_dir
        self._raw = raw_config

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self._dir

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._dir / "pyproject.toml"

    async def get_current_project_package_name(self) -> str:
        return "sample"

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        return self._raw

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        return self._raw

    def get_current_project_raw_config_version(self) -> int:
        return 0

    async def get_workspace_packages(self) -> dict[str, Any]:
        return {}


class _RecordingRunner(CommandRunner):
    """Real execution that records every process's stderr."""

    def __init__(self) -> None:
        slots = ProcessSlots(target=4)
        super().__init__(
            logger=NoOpLogger(),
            config=CommandRunnerConfig(max_concurrent_processes=4),
            process_slots=slots,
        )
        self.stderrs: list[str] = []
        self.exit_codes: list[int | None] = []

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        process = await super().run_to_completion(
            cmd,
            cwd=cwd,
            env=env,
            new_process_group=new_process_group,
            input=input,
            timeout=timeout,
        )
        self.stderrs.append(process.get_error_output() or process.get_output() or "")
        self.exit_codes.append(process.get_exit_code())
        return process


def _clean_uv_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ.keys()):
        if key == "VIRTUAL_ENV" or key.startswith("UV_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))


def _uv_python_json(args: list[str]) -> Any:
    completed = subprocess.run(
        [str(get_uv_executable()), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


async def _create_child(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_config: dict[str, Any],
    child: str,
    interpreter: str | None,
) -> tuple[Any, _RecordingRunner]:
    _clean_uv_env(tmp_path, monkeypatch)
    runner = _RecordingRunner()
    defs_dir = tmp_path / "defs"
    defs_dir.mkdir(exist_ok=True)
    target_file = defs_dir / "pyproject.toml"
    await write_project_config_dump(
        project_def_path=tmp_path / "pyproject.toml",
        target_file=target_file,
        action_runner=cast(Any, _DumpActionRunner()),
        project_info_provider=cast(Any, _ProjectInfoStub(tmp_path, raw_config)),
        logger=cast(Any, NoOpLogger()),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
    )
    env_info = EnvInfo(
        name=child,
        venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / child),
        project_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        interpreter=interpreter,
    )
    result = await run_handler(
        UvCreateEnvHandler,
        create_env_action.CreateEnvRunPayload(
            env=env_info,
            recreate=True,
            resolved_project_def_path=path_to_resource_uri(target_file),
        ),
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides={
            icommandrunner.ICommandRunner: runner,
            ilogger.ILogger: NoOpLogger(),
            ifilemanager.IFileManager: _RecordingFileManager(),
        },
    )
    assert result is not None
    return result, runner


class _RecordingFileManager:
    async def get_content(self, file_path: pathlib.Path) -> str:
        raise NotImplementedError

    async def get_file_version(self, file_path: pathlib.Path) -> str:
        raise NotImplementedError

    async def save_file(self, file_path: pathlib.Path, file_content: str) -> None:
        pass

    async def create_dir(
        self, dir_path: pathlib.Path, create_parents: bool = True, exist_ok: bool = True
    ) -> None:
        pass

    async def remove_dir(
        self, dir_path: pathlib.Path, *, tolerant: bool = False
    ) -> None:
        pass


def _venv_python_minor(venv_dir: pathlib.Path) -> str:
    candidates = [
        venv_dir / "bin" / "python",
        venv_dir / "Scripts" / "python.exe",
    ]
    exe = next(p for p in candidates if p.exists())
    out = subprocess.run(
        [str(exe), "-c", "import sys; print(f'{sys.version_info[1]}')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


async def test_every_matrix_child_is_created_without_a_parse_warning(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each matrix child is created with explicit `--python` and no parse warning.

    A `Failed to parse` in uv's stderr would mean the dumped `pyproject.toml`
    still carries a PEP 735-invalid group name, and the venv would fall back to
    an unconstrained interpreter.
    """
    _write_pyproject(tmp_path, ">=3.11", ["cpython@3.11", "cpython@3.12"])
    raw_config = _resolved_config(tmp_path)
    children = sorted(
        name
        for name, entry in raw_config["tool"]["finecode"]["env"].items()
        if isinstance(entry, dict) and "matrix_env" in entry
    )
    assert children == ["testing-cpython-3.11", "testing-cpython-3.12"]
    for child in children:
        interpreter = raw_config["tool"]["finecode"]["env"][child]["interpreter"]
        result, runner = await _create_child(
            tmp_path, monkeypatch, raw_config, child, interpreter
        )
        assert result.errors == []
        assert all("Failed to parse" not in err for err in runner.stderrs)
        minor = _venv_python_minor(tmp_path / "venvs" / child)
        assert minor == interpreter.split("@")[1].split(".")[1]


async def test_requires_python_changes_implicit_interpreter_selection(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-matrix env with no `--python` follows `requires-python`.

    X is an installed minor different from uv's unconstrained choice. The
    test env must therefore contain at least two installed minors.
    """
    _clean_uv_env(tmp_path, monkeypatch)
    installed = _uv_python_json(
        ["python", "list", "--only-installed", "--output-format", "json"]
    )
    minors = sorted(
        {
            f"{entry['version'].split('.')[0]}.{entry['version'].split('.')[1]}"
            for entry in installed
        }
    )
    if len(minors) < 2:
        pytest.skip("needs at least two installed Python minors")
    unconstrained = subprocess.run(
        [str(get_uv_executable()), "python", "find"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )
    assert unconstrained.returncode == 0, unconstrained.stderr
    chosen = next((m for m in minors if m not in unconstrained.stdout), minors[0])
    _write_pyproject(tmp_path, f"=={chosen}.*", ["cpython@3.11"])
    raw_config = _resolved_config(tmp_path)
    result, runner = await _create_child(tmp_path, monkeypatch, raw_config, "dev", None)
    assert result.errors == []
    assert all("Failed to parse" not in err for err in runner.stderrs)
    minor = _venv_python_minor(tmp_path / "venvs" / "dev")
    assert minor == chosen.split(".")[1]


async def test_child_outside_requires_python_is_created_with_a_warning(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child outside `requires-python` is still created when `--python` is explicit.

    uv warns about the incompatibility but exits 0, so the venv exists and no
    parse warning pollutes the output.
    """
    _write_pyproject(tmp_path, "==3.12.*", ["cpython@3.11", "cpython@3.12"])
    raw_config = _resolved_config(tmp_path)
    result, runner = await _create_child(
        tmp_path, monkeypatch, raw_config, "testing-cpython-3.11", "cpython@3.11"
    )
    assert result.errors == []
    assert all("Failed to parse" not in err for err in runner.stderrs)
    assert (tmp_path / "venvs" / "testing-cpython-3.11").exists()
    assert any("incompatible" in err.lower() for err in runner.stderrs)
