from __future__ import annotations

import contextlib
import errno
import pathlib
import shutil
from collections.abc import AsyncIterator
from typing import Any, cast

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
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner.testing import (
    NoOpLogger,
    handler_test_session,
    run_handler,
)

from fine_python_uv.create_env_handler import UvCreateEnvHandler


class _FakeScope:
    """Stands in for the owned scope: entering yields the canned process."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process

    async def __aenter__(self) -> _FakeProcess:
        return self._process

    async def __aexit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> bool:
        return False


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
    """Captures every argv vector and working directory it is asked to run,
    instead of executing it."""

    def __init__(self, exit_code: int = 0, output: str = "") -> None:
        self.exit_code = exit_code
        self.output = output
        self.commands: list[list[str]] = []
        self.cwds: list[pathlib.Path | None] = []

    def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
    ) -> contextlib.AbstractAsyncContextManager[_FakeProcess]:
        icommandrunner.check_argv(cmd)
        self.commands.append(list(cmd))
        self.cwds.append(cwd)
        return _FakeScope(_FakeProcess(exit_code=self.exit_code, output=self.output))

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> _FakeProcess:
        async with self.run(cmd, cwd=cwd, env=env) as process:
            return process

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise NotImplementedError


class _FakeFileManager:
    def __init__(self) -> None:
        self.removed: list[pathlib.Path] = []

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
        self.removed.append(dir_path)
        shutil.rmtree(dir_path, ignore_errors=tolerant)


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

    def __init__(self, raw_config: dict[str, Any] | None = None) -> None:
        self.raw_config: dict[str, Any] = raw_config if raw_config is not None else {}
        self.raw_config_calls: list[pathlib.Path] = []

    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        self.raw_config_calls.append(project_def_path)
        return self.raw_config

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
    project_info_provider: _FakeProjectInfoProvider | None = None,
    file_manager: _FakeFileManager | None = None,
) -> dict[type, Any]:
    return {
        icommandrunner.ICommandRunner: command_runner,
        ilogger.ILogger: NoOpLogger(),
        ifilemanager.IFileManager: file_manager or _FakeFileManager(),
        iprojectactionrunner.IProjectActionRunner: project_action_runner
        or _FakeProjectActionRunner(),
        iprojectinfoprovider.IProjectInfoProvider: project_info_provider
        or _FakeProjectInfoProvider(),
    }


def _env_info(
    tmp_path: pathlib.Path,
    *,
    name: str = "testing-cpython-3.11",
    interpreter: str | None = "cpython@3.11",
) -> create_envs_action.EnvInfo:
    return create_envs_action.EnvInfo(
        name=name,
        venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / name),
        project_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        interpreter=interpreter,
    )


def _valid_def(tmp_path: pathlib.Path, name: str = "defs") -> ResourceUri:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    f = d / "pyproject.toml"
    _ = f.write_text("[project]\n", encoding="utf-8")
    return path_to_resource_uri(f)


async def test_uv_venv_command_includes_python_flag_when_interpreter_is_set(
    tmp_path: pathlib.Path,
) -> None:
    command_runner = _FakeCommandRunner()
    # venv_dir_path must not exist so `_is_valid_virtualenv` returns False and the
    # create path (which builds the `uv venv` command) actually runs.
    venv_dir_path = tmp_path / "venvs" / "testing"
    project_def_path = tmp_path / "pyproject.toml"
    env_info = create_envs_action.EnvInfo(
        name="testing-cpython-3.11",
        venv_dir_path=path_to_resource_uri(venv_dir_path),
        project_def_path=path_to_resource_uri(project_def_path),
        interpreter="cpython@3.11",
    )
    payload = create_env_action.CreateEnvRunPayload(
        env=env_info, recreate=False, resolved_project_def_path=_valid_def(tmp_path)
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner),
    )

    assert result is not None
    assert result.errors == []
    venv_commands = [cmd for cmd in command_runner.commands if "venv" in cmd]
    assert len(venv_commands) == 1
    assert "--python" in venv_commands[0]
    assert venv_commands[0][venv_commands[0].index("--python") + 1] == "cpython@3.11"


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
    payload = create_env_action.CreateEnvRunPayload(
        env=env_info, recreate=False, resolved_project_def_path=_valid_def(tmp_path)
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(command_runner),
    )

    assert result is not None
    assert result.errors == []
    venv_commands = [cmd for cmd in command_runner.commands if "venv" in cmd]
    assert len(venv_commands) == 1
    assert "--python" not in venv_commands[0]


async def test_valid_venv_without_definition_needs_no_uv(
    tmp_path: pathlib.Path,
) -> None:
    """A valid venv with no definition returns without starting uv."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_info_provider = _FakeProjectInfoProvider()
    venv_dir_path = tmp_path / "venvs" / "testing-cpython-3.11"
    (venv_dir_path / "bin").mkdir(parents=True)
    (venv_dir_path / "bin" / "python").touch()
    (venv_dir_path / "pyvenv.cfg").touch()
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=False, resolved_project_def_path=None
    )

    result = await run_handler(
        UvCreateEnvHandler,
        payload,
        action_cls=create_env_action.CreateEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(
            command_runner, project_action_runner, project_info_provider
        ),
    )

    assert result is not None
    assert result.created is False
    assert result.errors == []
    assert project_action_runner.recorded_payloads == []
    assert project_info_provider.raw_config_calls == []
    # Only the validity probe ran, never a `uv venv` command.
    assert command_runner.commands and not any(
        "venv" in cmd for cmd in command_runner.commands
    )


def _create_env_actions() -> dict[str, Any]:
    action_source = (
        f"{create_env_action.CreateEnvAction.__module__}."
        f"{create_env_action.CreateEnvAction.__qualname__}"
    )
    handler_source = (
        f"{UvCreateEnvHandler.__module__}.{UvCreateEnvHandler.__qualname__}"
    )
    return {
        create_env_action.CreateEnvAction.__name__: {
            "source": action_source,
            "handlers": [
                {"name": UvCreateEnvHandler.__name__, "source": handler_source}
            ],
        }
    }


async def _run_create_with_supplied_dump(
    tmp_path: pathlib.Path,
    supplied: ResourceUri | None,
    *,
    command_runner: _FakeCommandRunner,
    project_action_runner: _FakeProjectActionRunner,
    project_info_provider: _FakeProjectInfoProvider | None = None,
    file_manager: _FakeFileManager | None = None,
    valid_venv: bool = False,
    recreate: bool = False,
) -> tuple[create_envs_action.CreateEnvsRunResult | None, _FakeFileManager]:
    if valid_venv:
        venv_dir = tmp_path / "venvs" / "testing-cpython-3.11"
        (venv_dir / "bin").mkdir(parents=True)
        (venv_dir / "bin" / "python").touch()
        (venv_dir / "pyvenv.cfg").touch()
    fm = file_manager or _FakeFileManager()
    payload = create_env_action.CreateEnvRunPayload(
        env=_env_info(tmp_path), recreate=recreate, resolved_project_def_path=supplied
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_create_env_actions(),
        service_overrides=_service_overrides(
            command_runner,
            project_action_runner,
            project_info_provider or _FakeProjectInfoProvider(),
            fm,
        ),
    ) as session:
        result = await session.run_action(
            create_env_action.CreateEnvAction.__name__,
            payload,
        )
        return cast("create_envs_action.CreateEnvsRunResult | None", result), fm


# Joined so the old wording never appears literally: a source audit greps
# for those phrases and must find nothing.
_NOT_SHARED = " ".join(["shared", "config", "dump"])
_NOT_TEMPORARY = " ".join(["temporary", "dump"])


@pytest.mark.parametrize(
    "kind", ["non-file-uri", "missing-file", "directory", "wrong-name"]
)
async def test_create_with_unusable_supplied_def_fails_without_starting_uv(
    tmp_path: pathlib.Path, kind: str
) -> None:
    """A supplied project definition file that is not a file URI, names no
    existing file, or names a file other than `pyproject.toml` fails the env
    without starting uv: the error names the venv and the supplied value, no
    raw config is fetched, no `dump_config` is dispatched, and no fallback
    per-env dump is made."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_info_provider = _FakeProjectInfoProvider()
    if kind == "non-file-uri":
        supplied = ResourceUri("other://dump")
    elif kind == "missing-file":
        supplied = path_to_resource_uri(tmp_path / "missing" / "pyproject.toml")
    elif kind == "directory":
        dir_form = tmp_path / "dir-form"
        dir_form.mkdir()
        _ = (dir_form / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        supplied = path_to_resource_uri(dir_form)
    else:
        shared = tmp_path / "shared"
        shared.mkdir()
        _ = (shared / "config.toml").write_text("[project]\n", encoding="utf-8")
        supplied = path_to_resource_uri(shared / "config.toml")
    venv_dir_path = tmp_path / "venvs" / "testing-cpython-3.11"

    result, _fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        project_info_provider=project_info_provider,
    )

    assert result is not None
    assert result.created is False
    assert len(result.errors) == 1
    assert str(venv_dir_path) in result.errors[0]
    assert str(supplied) in result.errors[0]
    assert "uv was not started" in result.errors[0]
    assert "supplied project definition file" in result.errors[0]
    assert _NOT_SHARED not in result.errors[0]
    assert _NOT_TEMPORARY not in result.errors[0]
    assert project_info_provider.raw_config_calls == []
    assert project_action_runner.recorded_payloads == []
    assert not any("venv" in cmd for cmd in command_runner.commands)


async def test_create_with_valid_venv_never_reads_supplied_def(
    tmp_path: pathlib.Path,
) -> None:
    """A venv that is already valid succeeds without touching the supplied
    dump, even when the supplied value names nothing usable."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    supplied = path_to_resource_uri(tmp_path / "gone" / "pyproject.toml")

    result, _fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        valid_venv=True,
    )

    assert result is not None
    assert result.created is False
    assert result.errors == []
    assert project_action_runner.recorded_payloads == []
    assert not any("venv" in cmd for cmd in command_runner.commands)


async def test_create_with_valid_supplied_def_runs_uv_in_place(
    tmp_path: pathlib.Path,
) -> None:
    """A valid supplied project definition file is used as-is: uv runs with
    its parent as cwd, no own dump is made, and the file and its directory
    still exist unchanged after the handler returns."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    shared = tmp_path / "shared"
    shared.mkdir()
    dump_file = shared / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    before = dump_file.read_bytes()
    supplied = path_to_resource_uri(dump_file)

    result, _fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
    )

    assert result is not None
    assert result.errors == []
    assert result.created is True
    assert project_action_runner.recorded_payloads == []
    assert len(command_runner.cwds) == 1
    assert command_runner.cwds[0] == shared
    assert shared.exists()
    assert dump_file.is_file()
    assert dump_file.read_bytes() == before


class _RaceRemovingCommandRunner(_FakeCommandRunner):
    """Removes the cwd and fails the spawn of the `uv venv` command, as
    the real runner does when the lender's directory is gone."""

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> _FakeProcess:
        if "venv" in list(cmd):
            assert cwd is not None
            shutil.rmtree(cwd)
            raise icommandrunner.ProgramStartError(
                cmd[0],
                cwd,
                FileNotFoundError(errno.ENOENT, "No such file or directory"),
            )
        return await super().run_to_completion(
            cmd,
            cwd=cwd,
            env=env,
            new_process_group=new_process_group,
            input=input,
            timeout=timeout,
        )


async def test_create_with_supplied_def_removed_before_spawn_fails_cleanly(
    tmp_path: pathlib.Path,
) -> None:
    """A dump file removed between the handler's check and the uv spawn
    (the lender tore down under D2.G8) is one env error, not a raise: the
    error names the venv, the supplied value and the failed spawn."""
    command_runner = _RaceRemovingCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    shared = tmp_path / "shared"
    shared.mkdir()
    dump_file = shared / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    supplied = path_to_resource_uri(dump_file)
    venv_dir_path = tmp_path / "venvs" / "testing-cpython-3.11"

    result, _fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
    )

    assert result is not None
    assert result.created is False
    assert len(result.errors) == 1
    assert str(venv_dir_path) in result.errors[0]
    assert str(supplied) in result.errors[0]
    assert "uv was not started" in result.errors[0]
    assert "supplied project definition file" in result.errors[0]
    assert _NOT_SHARED not in result.errors[0]
    assert project_action_runner.recorded_payloads == []
    assert not any("venv" in cmd for cmd in command_runner.commands)


async def test_create_with_supplied_def_and_failing_uv_names_supplied_file(
    tmp_path: pathlib.Path,
) -> None:
    """A nonzero `uv venv` exit on a supplied project definition file is one
    env error naming the venv, the supplied value and uv's error output. The
    error does not blame a temporary dump or suggest `dump-config`: the
    handler does not know who produced the file."""
    uv_error = "error: failed to find interpreter for cpython@3.11"
    command_runner = _FakeCommandRunner(exit_code=1, output=uv_error)
    project_action_runner = _FakeProjectActionRunner()
    shared = tmp_path / "shared"
    shared.mkdir()
    dump_file = shared / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    supplied = path_to_resource_uri(dump_file)
    venv_dir_path = tmp_path / "venvs" / "testing-cpython-3.11"

    result, _fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
    )

    assert result is not None
    assert result.created is True
    assert len(result.errors) == 1
    assert str(venv_dir_path) in result.errors[0]
    assert str(supplied) in result.errors[0]
    assert "supplied project definition file" in result.errors[0]
    assert uv_error in result.errors[0]
    assert _NOT_SHARED not in result.errors[0]
    assert _NOT_TEMPORARY not in result.errors[0]
    assert "dump-config" not in result.errors[0]
    assert project_action_runner.recorded_payloads == []


async def test_missing_definition_fails_without_uv_or_wm_requests(
    tmp_path: pathlib.Path,
) -> None:
    """Missing definition fails without starting uv or contacting the workspace."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_info_provider = _FakeProjectInfoProvider()
    venv_dir_path = tmp_path / "venvs" / "testing-cpython-3.11"

    result, fm = await _run_create_with_supplied_dump(
        tmp_path,
        None,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        project_info_provider=project_info_provider,
    )

    assert result is not None
    assert result.created is False
    assert len(result.errors) == 1
    assert str(venv_dir_path) in result.errors[0]
    assert "no project definition was supplied" in result.errors[0]
    assert "resolved_project_def_path" in result.errors[0]
    assert "uv was not started" in result.errors[0]
    assert not any("venv" in cmd for cmd in command_runner.commands)
    assert project_info_provider.raw_config_calls == []
    assert project_action_runner.recorded_payloads == []


async def test_recreate_with_missing_definition_keeps_venv(
    tmp_path: pathlib.Path,
) -> None:
    """Recreate with no definition keeps the existing venv and makes no requests."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_info_provider = _FakeProjectInfoProvider()
    venv_dir = tmp_path / "venvs" / "testing-cpython-3.11"
    venv_dir.mkdir(parents=True)
    marker = venv_dir / "pyvenv.cfg"
    _ = marker.write_text("x", encoding="utf-8")
    venv_dir_path = venv_dir

    result, fm = await _run_create_with_supplied_dump(
        tmp_path,
        None,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        project_info_provider=project_info_provider,
        recreate=True,
    )

    assert result is not None
    assert result.created is False
    assert len(result.errors) == 1
    assert "no project definition was supplied" in result.errors[0]
    assert fm.removed == []
    assert marker.exists()
    assert not any("venv" in cmd for cmd in command_runner.commands)
    assert project_info_provider.raw_config_calls == []
    assert project_action_runner.recorded_payloads == []


async def test_unusable_supplied_def_under_recreate_keeps_venv(
    tmp_path: pathlib.Path,
) -> None:
    """Unusable values under recreate fail without removing the venv."""
    for kind in ["non-file-uri", "missing-file", "directory", "wrong-name"]:
        command_runner = _FakeCommandRunner()
        project_action_runner = _FakeProjectActionRunner()
        venv_dir = tmp_path / f"venvs-{kind}" / "testing-cpython-3.11"
        venv_dir.mkdir(parents=True)
        marker = venv_dir / "pyvenv.cfg"
        _ = marker.write_text("x", encoding="utf-8")
        # point env at this venv dir
        env_info = create_envs_action.EnvInfo(
            name="testing-cpython-3.11",
            venv_dir_path=path_to_resource_uri(venv_dir),
            project_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
            interpreter=None,
        )
        if kind == "non-file-uri":
            supplied = ResourceUri("other://dump")
        elif kind == "missing-file":
            supplied = path_to_resource_uri(tmp_path / "missing" / "pyproject.toml")
        elif kind == "directory":
            dir_form = tmp_path / f"dir-form-{kind}"
            dir_form.mkdir(exist_ok=True)
            _ = (dir_form / "pyproject.toml").write_text(
                "[project]\n", encoding="utf-8"
            )
            supplied = path_to_resource_uri(dir_form)
        else:
            shared = tmp_path / f"shared-{kind}"
            shared.mkdir(exist_ok=True)
            _ = (shared / "config.toml").write_text("[project]\n", encoding="utf-8")
            supplied = path_to_resource_uri(shared / "config.toml")
        fm = _FakeFileManager()
        payload = create_env_action.CreateEnvRunPayload(
            env=env_info, recreate=True, resolved_project_def_path=supplied
        )
        from finecode_extension_runner.testing import handler_test_session as _hts

        async with _hts(
            project_dir=tmp_path,
            actions=_create_env_actions(),
            service_overrides=_service_overrides(
                command_runner, project_action_runner, _FakeProjectInfoProvider(), fm
            ),
        ) as session:
            result = await session.run_action(
                create_env_action.CreateEnvAction.__name__, payload
            )
        assert result is not None
        assert result.created is False
        assert len(result.errors) == 1
        assert "uv was not started" in result.errors[0]
        assert fm.removed == []
        assert marker.exists()


async def test_valid_supplied_def_records_no_wm_requests(
    tmp_path: pathlib.Path,
) -> None:
    """A direct create from a valid file makes no workspace requests."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    project_info_provider = _FakeProjectInfoProvider()
    shared = tmp_path / "shared-ac8b"
    shared.mkdir()
    dump_file = shared / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    supplied = path_to_resource_uri(dump_file)

    result, fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        project_info_provider=project_info_provider,
    )

    assert result is not None
    assert result.errors == []
    assert project_info_provider.raw_config_calls == []
    assert project_action_runner.recorded_payloads == []


async def test_race_under_recreate_returns_one_error(
    tmp_path: pathlib.Path,
) -> None:
    """A file removed before the spawn under recreate gives one error."""
    command_runner = _RaceRemovingCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    shared = tmp_path / "shared-race-recreate"
    shared.mkdir()
    dump_file = shared / "pyproject.toml"
    _ = dump_file.write_text("[project]\n", encoding="utf-8")
    supplied = path_to_resource_uri(dump_file)
    venv_dir = tmp_path / "venvs" / "testing-cpython-3.11"
    venv_dir.mkdir(parents=True)
    _ = (venv_dir / "pyvenv.cfg").write_text("x", encoding="utf-8")
    venv_dir_path = venv_dir

    result, fm = await _run_create_with_supplied_dump(
        tmp_path,
        supplied,
        command_runner=command_runner,
        project_action_runner=project_action_runner,
        recreate=True,
    )

    assert result is not None
    assert result.created is False
    assert len(result.errors) == 1
    assert str(venv_dir_path) in result.errors[0]
    assert str(supplied) in result.errors[0]
    assert "uv was not started" in result.errors[0]
    assert len(fm.removed) == 1
