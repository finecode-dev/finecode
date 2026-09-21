from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fine_envs import create_env_action, create_envs_action, dump_config_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ifilemanager,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import NoOpLogger, run_handler

from fine_python_uv.create_env_handler import UvCreateEnvHandler


class _FakeProcess:
    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self._exit_code = exit_code
        self._output = output

    def get_exit_code(self) -> int | None:
        return self._exit_code

    def get_output(self) -> str:
        return self._output

    def get_error_output(self) -> str:
        return ""

    def write_to_stdin(self, value: str) -> None:
        pass

    def close_stdin(self) -> None:
        pass

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self.get_output().splitlines():
            yield line

    async def stderr_lines(self) -> AsyncIterator[str]:
        for line in self.get_error_output().splitlines():
            yield line

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeCommandRunner:
    """Captures every command string and working directory it is asked to run,
    instead of executing it."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.commands: list[str] = []
        self.cwds: list[pathlib.Path | None] = []

    async def run(
        self,
        cmd: str,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        self.commands.append(cmd)
        self.cwds.append(cwd)
        return _FakeProcess(exit_code=self.exit_code, output="")

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

    async def remove_dir(
        self, dir_path: pathlib.Path, *, tolerant: bool = False
    ) -> None:
        pass


class _FakeProjectActionRunner:
    """Records every dumped payload; the handler's config-dump step doesn't need
    a real DumpConfigAction handler registered in the test session."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.recorded_payloads: list[dump_config_action.DumpConfigRunPayload] = []

    async def get_actions_for_parent(self, parent_action_type: type) -> dict[str, Any]:
        raise NotImplementedError

    async def run_action(
        self,
        action_type: Any,
        payload: dump_config_action.DumpConfigRunPayload,
        meta: Any,
        caller_kwargs: Any = None,
    ) -> None:
        self.recorded_payloads.append(payload)
        if self._error is not None:
            raise self._error

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

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        return {}

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_packages(
        self,
    ) -> dict[str, iprojectinfoprovider.WorkspacePackage]:
        raise NotImplementedError


def _service_overrides(
    command_runner: _FakeCommandRunner,
    project_action_runner: _FakeProjectActionRunner | None = None,
) -> dict[type, Any]:
    return {
        icommandrunner.ICommandRunner: command_runner,
        ilogger.ILogger: NoOpLogger(),
        ifilemanager.IFileManager: _FakeFileManager(),
        iprojectactionrunner.IProjectActionRunner: project_action_runner
        or _FakeProjectActionRunner(),
        iprojectinfoprovider.IProjectInfoProvider: _FakeProjectInfoProvider(),
    }


def _env_info(
    tmp_path: pathlib.Path,
    *,
    name: str = "testing@cpython-3.11",
    interpreter: str | None = "cpython@3.11",
) -> create_envs_action.EnvInfo:
    return create_envs_action.EnvInfo(
        name=name,
        venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / name),
        project_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        interpreter=interpreter,
    )


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


async def test_create_dumps_config_once_to_a_private_temp_dir(
    tmp_path: pathlib.Path,
) -> None:
    """The config the handler feeds to uv is machine input for a single run:
    it is dumped exactly once, unformatted, to a directory outside the
    project, and the uv command runs with that directory as its working
    directory. The directory is gone after the run — no `finecode_config_dump/`
    is ever produced by env creation."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_def_path = tmp_path / "pyproject.toml"
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=False
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner, project_action_runner),
    )

    assert result is not None
    assert result.errors == []
    assert len(project_action_runner.recorded_payloads) == 1
    dumped = project_action_runner.recorded_payloads[0]
    assert dumped.format_output is False
    assert dumped.source_file_path == path_to_resource_uri(project_def_path)
    dump_dir = resource_uri_to_path(dumped.target_file_path).parent
    assert dump_dir.name.startswith("finecode_uv_config_")
    assert not dump_dir.is_relative_to(tmp_path)
    assert len(command_runner.cwds) == 1
    assert command_runner.cwds[0] == dump_dir
    assert not dump_dir.exists()


async def test_create_uv_failure_removes_the_temp_dump_and_names_dump_config(
    tmp_path: pathlib.Path,
) -> None:
    """A failing uv run still cleans up the temporary config dump, and the
    error tells the user how to inspect what uv ran with, since the dump is
    gone by the time they read it."""
    command_runner = _FakeCommandRunner(exit_code=1)
    project_action_runner = _FakeProjectActionRunner()
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=False
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner, project_action_runner),
    )

    assert result is not None
    assert result.errors
    assert "Failed to create virtualenv" in result.errors[0]
    assert "dump-config" in result.errors[0]
    dump_dir = resource_uri_to_path(
        project_action_runner.recorded_payloads[0].target_file_path
    ).parent
    assert not dump_dir.exists()


async def test_create_dump_failure_propagates_and_removes_the_temp_dir(
    tmp_path: pathlib.Path,
) -> None:
    """A failed `dump_config` dispatch is not something env creation can
    recover from: it propagates as before, and the temp dir is still removed
    on the way out."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner(
        error=iprojectactionrunner.ActionRunFailed("dump failed")
    )
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=False
    )

    with pytest.raises(run_action_service.ActionFailedException) as exc_info:
        await run_handler(
            UvCreateEnvHandler,
            payload,
            action_cls=create_env_action.CreateEnvAction,
            project_dir=tmp_path,
            service_overrides=_service_overrides(command_runner, project_action_runner),
        )

    assert "dump failed" in exc_info.value.message
    dump_dir = resource_uri_to_path(
        project_action_runner.recorded_payloads[0].target_file_path
    ).parent
    assert not dump_dir.exists()


async def test_valid_venv_never_dumps_config(tmp_path: pathlib.Path) -> None:
    """A venv that is already valid needs no uv run and therefore no config
    dump at all — the dump is an input to uv, not a side effect of env
    creation."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    venv_dir_path = tmp_path / "venvs" / "testing@cpython-3.11"
    (venv_dir_path / "bin").mkdir(parents=True)
    (venv_dir_path / "bin" / "python").touch()
    (venv_dir_path / "pyvenv.cfg").touch()
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=False
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner, project_action_runner),
    )

    assert result is not None
    assert result.created is False
    assert result.errors == []
    assert project_action_runner.recorded_payloads == []
    # Only the validity probe ran, never a `uv venv` command.
    assert command_runner.commands and not any(
        " venv " in cmd for cmd in command_runner.commands
    )
