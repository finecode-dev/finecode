from __future__ import annotations

import pathlib
from typing import Any

from fine_envs import create_env_action, create_envs_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ifilemanager,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger, run_handler

from fine_python_uv.create_env_handler import UvCreateEnvHandler


class _FakeProcess:
    def get_exit_code(self) -> int | None:
        return 0

    def get_output(self) -> str:
        return ""

    def get_error_output(self) -> str:
        return ""

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeCommandRunner:
    """Captures every command string it is asked to run, instead of executing it."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        self.commands.append(cmd)
        return _FakeProcess()

    def run_sync(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise NotImplementedError


class _FakeFileManager:
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

    async def remove_dir(self, dir_path: pathlib.Path) -> None:
        pass


class _FakeProjectActionRunner:
    """No-ops `run_action` so the handler's config-dump step doesn't need a real
    DumpConfigAction handler registered in the test session."""

    async def get_actions_for_parent(self, parent_action_type: type) -> dict[str, Any]:
        raise NotImplementedError

    async def run_action(
        self,
        action_type: Any,
        payload: Any,
        meta: Any,
        caller_kwargs: Any = None,
    ) -> None:
        return None

    def run_action_iter(
        self,
        action_type: Any,
        payload: Any,
        meta: Any,
        caller_kwargs: Any = None,
    ) -> Any:
        raise NotImplementedError


class _FakeProjectInfoProvider:
    """Only `get_project_raw_config` is exercised (by the config-dump step)."""

    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(self, project_def_path: pathlib.Path) -> dict[str, Any]:
        return {}

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_editable_packages(self) -> dict[str, pathlib.Path]:
        raise NotImplementedError


def _service_overrides(command_runner: _FakeCommandRunner) -> dict[type, Any]:
    return {
        icommandrunner.ICommandRunner: command_runner,
        ilogger.ILogger: NoOpLogger(),
        ifilemanager.IFileManager: _FakeFileManager(),
        iprojectactionrunner.IProjectActionRunner: _FakeProjectActionRunner(),
        iprojectinfoprovider.IProjectInfoProvider: _FakeProjectInfoProvider(),
    }


async def test_uv_venv_command_includes_python_flag_when_interpreter_is_set(
    tmp_path: pathlib.Path,
) -> None:
    command_runner = _FakeCommandRunner()
    # venv_dir_path must not exist so `_is_valid_virtualenv` returns False and the
    # create path (which builds the `uv venv` command) actually runs.
    venv_dir_path = tmp_path / "venvs" / "testing"
    project_def_path = tmp_path / "pyproject.toml"
    env_info = create_envs_action.EnvInfo(
        name="testing@cpython-3.11",
        venv_dir_path=path_to_resource_uri(venv_dir_path),
        project_def_path=path_to_resource_uri(project_def_path),
        interpreter="cpython@3.11",
    )
    payload = create_env_action.CreateEnvRunPayload(env=env_info, recreate=False)

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner),
    )

    assert result is not None
    assert result.errors == []
    venv_commands = [cmd for cmd in command_runner.commands if " venv " in cmd]
    assert len(venv_commands) == 1
    assert '--python "cpython@3.11"' in venv_commands[0]


async def test_uv_venv_command_omits_python_flag_when_interpreter_is_none(
    tmp_path: pathlib.Path,
) -> None:
    command_runner = _FakeCommandRunner()
    venv_dir_path = tmp_path / "venvs" / "dev"
    project_def_path = tmp_path / "pyproject.toml"
    env_info = create_envs_action.EnvInfo(
        name="dev",
        venv_dir_path=path_to_resource_uri(venv_dir_path),
        project_def_path=path_to_resource_uri(project_def_path),
        interpreter=None,
    )
    payload = create_env_action.CreateEnvRunPayload(env=env_info, recreate=False)

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner),
    )

    assert result is not None
    assert result.errors == []
    venv_commands = [cmd for cmd in command_runner.commands if " venv " in cmd]
    assert len(venv_commands) == 1
    assert "--python" not in venv_commands[0]
