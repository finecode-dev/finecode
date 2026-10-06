import pathlib
from typing import Any

from fine_envs import dump_config_action, install_deps_in_env_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import (
    NoOpLogger,
    nonexistent_abs_path,
    run_handler,
)

from fine_python_uv.install_deps_in_env_handler import (
    UvInstallDepsInEnvHandler,
    UvInstallDepsInEnvHandlerConfig,
)

_VENV_DIR = nonexistent_abs_path("venv")


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

    async def stdout_lines(self) -> Any:
        for line in self.get_output().splitlines():
            yield line

    async def stderr_lines(self) -> Any:
        for line in self.get_error_output().splitlines():
            yield line

    async def wait_for_end(self, timeout: float | None = None) -> None:
        pass


class _FakeCommandRunner:
    """Captures every argv vector and working directory it is asked to run,
    instead of executing it."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.commands: list[list[str]] = []
        self.cwds: list[pathlib.Path | None] = []

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        icommandrunner.check_argv(cmd)
        self.commands.append(list(cmd))
        self.cwds.append(cwd)
        return _FakeProcess(exit_code=self.exit_code, output="")

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> _FakeProcess:
        raise NotImplementedError


class _FakeProjectActionRunner:
    """Records every dispatched payload, so a test can assert none was sent."""

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
    """Counts the config fetches the handler makes, so a test can assert it makes none."""

    def __init__(self) -> None:
        self.raw_config_calls = 0
        self.extra_selection_calls = 0

    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        self.raw_config_calls += 1
        return {}

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        self.extra_selection_calls += 1
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
    project_action_runner: _FakeProjectActionRunner,
    project_info_provider: _FakeProjectInfoProvider,
) -> dict[type, Any]:
    return {
        icommandrunner.ICommandRunner: command_runner,
        ilogger.ILogger: NoOpLogger(),
        iprojectactionrunner.IProjectActionRunner: project_action_runner,
        iprojectinfoprovider.IProjectInfoProvider: project_info_provider,
    }


def _run_payload(
    tmp_path: pathlib.Path,
) -> install_deps_in_env_action.InstallDepsInEnvRunPayload:
    return install_deps_in_env_action.InstallDepsInEnvRunPayload(
        env_name="dev",
        venv_dir_path=path_to_resource_uri(tmp_path / ".venvs" / "dev"),
        project_dir_path=path_to_resource_uri(tmp_path),
        dependencies=[],
    )


def _handler(editable_mode: str | None = None) -> UvInstallDepsInEnvHandler:
    return UvInstallDepsInEnvHandler(
        config=UvInstallDepsInEnvHandlerConfig(editable_mode=editable_mode),
        command_runner=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
    )


def _dep(
    name: str,
    version_or_source: str,
    *,
    editable: bool = False,
    extras: list[str] | None = None,
) -> install_deps_in_env_action.Dependency:
    return install_deps_in_env_action.Dependency(
        name=name,
        version_or_source=version_or_source,
        editable=editable,
        extras=extras or [],
    )


def test_uv_editable_dep_emits_extras() -> None:
    """An editable spec with extras renders the bracket group before the file URI."""
    cmd = _handler()._construct_uv_install_cmd(
        uv_executable=pathlib.Path("uv"),
        venv_dir_path=_VENV_DIR,
        dependencies=[_dep("pkg", " @ file:///tmp/pkg", editable=True, extras=["a"])],
    )

    assert "pkg[a] @ file:///tmp/pkg" in cmd


def test_uv_non_editable_dep_emits_extras() -> None:
    cmd = _handler()._construct_uv_install_cmd(
        uv_executable=pathlib.Path("uv"),
        venv_dir_path=_VENV_DIR,
        dependencies=[_dep("pkg", "~=1.0", extras=["a"])],
    )

    assert "pkg[a]~=1.0" in cmd


def test_uv_cmd_argv_is_exact() -> None:
    """Each requirement is one argv element and no token carries quoting.

    The quoting this test used to assert (double-quoting requirements for
    cmd.exe) went away with the argv API: no shell parses these arguments, so
    on every platform uv receives exactly these tokens.
    """
    cmd = _handler(editable_mode="compat")._construct_uv_install_cmd(
        uv_executable=pathlib.Path("uv"),
        venv_dir_path=_VENV_DIR,
        dependencies=[
            _dep("pkg", " @ file:///D:/a/pkg", editable=True, extras=["a"]),
            _dep("other", ">=1.0"),
        ],
    )

    assert cmd == [
        "uv",
        "--no-config",
        "pip",
        "install",
        "--python",
        str(_VENV_DIR),
        "-C",
        "editable_mode=compat",
        "-e",
        "pkg[a] @ file:///D:/a/pkg",
        "other>=1.0",
    ]


async def test_install_runs_uv_in_project_dir_without_fetching_or_dumping_config(
    tmp_path: pathlib.Path,
) -> None:
    """uv runs with --no-config, so a dumped config would never be read; the
    dependency list on the command line is uv's complete input."""
    command_runner = _FakeCommandRunner()
    project_action_runner = _FakeProjectActionRunner()
    info_provider = _FakeProjectInfoProvider()

    result = await run_handler(
        UvInstallDepsInEnvHandler,
        _run_payload(tmp_path),
        action_cls=install_deps_in_env_action.InstallDepsInEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(
            command_runner, project_action_runner, info_provider
        ),
    )

    assert result is not None
    assert result.errors == []
    assert project_action_runner.recorded_payloads == []
    assert info_provider.raw_config_calls == 0
    assert info_provider.extra_selection_calls == 0
    assert command_runner.cwds == [tmp_path]
    assert len(command_runner.commands) == 1
    assert command_runner.commands[0][1] == "--no-config"
    assert not (tmp_path / "finecode_config_dump").exists()


async def test_install_uv_failure_names_the_project(
    tmp_path: pathlib.Path,
) -> None:
    """A failing uv run names the project it ran in."""
    command_runner = _FakeCommandRunner(exit_code=1)
    project_action_runner = _FakeProjectActionRunner()
    info_provider = _FakeProjectInfoProvider()

    result = await run_handler(
        UvInstallDepsInEnvHandler,
        _run_payload(tmp_path),
        action_cls=install_deps_in_env_action.InstallDepsInEnvAction,
        project_dir=tmp_path,
        service_overrides=_service_overrides(
            command_runner, project_action_runner, info_provider
        ),
    )

    assert result is not None
    assert result.errors
    assert f"for project {tmp_path}" in result.errors[0]
    assert "dump-config" not in result.errors[0]
    assert "temporary dump" not in result.errors[0]
