import dataclasses
import pathlib
import tempfile
from typing import Any, cast

from fine_envs import (
    create_env_action,
    create_envs_action,
    dump_config_action,
)
from fine_envs.create_envs_dispatch_handler import CreateEnvsDispatchHandler
from fine_envs.dump_config_handler import DumpConfigHandler
from fine_envs.dump_config_save_handler import DumpConfigSaveHandler
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger, handler_test_session

from fine_python_uv.create_env_handler import UvCreateEnvHandler

from .test_create_env_handler import _FakeCommandRunner


class _DumpSeeingCommandRunner(_FakeCommandRunner):
    """Fake command runner that also records whether the working directory
    held a rendered `pyproject.toml` at the time of each run."""

    def __init__(self) -> None:
        super().__init__()
        self.saw_pyproject: list[bool] = []
        self.marker_seen: list[bool] = []

    def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
    ) -> Any:
        dump_file = cwd / "pyproject.toml" if cwd is not None else None
        self.saw_pyproject.append(dump_file is not None and dump_file.is_file())
        self.marker_seen.append(
            dump_file is not None
            and dump_file.is_file()
            and "tool-shared-dump-marker" in dump_file.read_text(encoding="utf-8")
        )
        return super().run(cmd, cwd=cwd, env=env, new_process_group=new_process_group)


class _CountingProjectInfoProvider:
    def __init__(self, raw: dict[str, Any], def_path: pathlib.Path) -> None:
        self.raw = raw
        self.def_path = def_path
        self.raw_calls: list[pathlib.Path] = []
        self.selection_calls = 0

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self.def_path.parent

    def get_current_project_def_path(self) -> pathlib.Path:
        return self.def_path

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        self.raw_calls.append(project_def_path)
        return self.raw

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        return 0

    async def get_workspace_packages(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        self.selection_calls += 1
        return {}


def _source(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _session_actions(
    dump_handler_classes: list[type],
) -> dict[str, Any]:
    def _handler(name: str, cls: type) -> dict[str, str]:
        return {"name": name, "source": _source(cls), "env": "test"}

    return {
        create_envs_action.CreateEnvsAction.__name__: {
            "source": _source(create_envs_action.CreateEnvsAction),
            "handlers": [_handler("dispatch", CreateEnvsDispatchHandler)],
        },
        create_env_action.CreateEnvAction.__name__: {
            "source": _source(create_env_action.CreateEnvAction),
            "handlers": [_handler("uv", UvCreateEnvHandler)],
        },
        dump_config_action.DumpConfigAction.__name__: {
            "source": _source(dump_config_action.DumpConfigAction),
            "handlers": [_handler(cls.__name__, cls) for cls in dump_handler_classes],
        },
    }


def _env_info(
    def_path: pathlib.Path, tmp_path: pathlib.Path, name: str
) -> create_envs_action.EnvInfo:
    return create_envs_action.EnvInfo(
        name=name,
        venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / name),
        project_def_path=path_to_resource_uri(def_path),
        interpreter="cpython@3.11",
    )


async def _run_batch(
    tmp_path: pathlib.Path,
    count: int,
    *,
    command_runner: _DumpSeeingCommandRunner,
    provider: _CountingProjectInfoProvider,
    dump_handler_classes: list[type],
) -> create_envs_action.CreateEnvsRunResult | None:
    envs = [_env_info(provider.def_path, tmp_path, f"env-{i}") for i in range(count)]
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(dump_handler_classes),
        service_overrides={
            icommandrunner.ICommandRunner: command_runner,
            ilogger.ILogger: NoOpLogger(),
            iprojectinfoprovider.IProjectInfoProvider: provider,
        },
    ) as session:
        result = await session.run_action(
            create_envs_action.CreateEnvsAction.__name__,
            create_envs_action.CreateEnvsRunPayload(envs=envs),
        )
        return cast("create_envs_action.CreateEnvsRunResult | None", result)


async def test_three_creations_share_one_dump_and_cwd(tmp_path: pathlib.Path) -> None:
    """Three missing venvs are created from a single shared dump: one raw
    fetch, one dump dispatch, and every uv run sees the same live directory,
    which is gone when the batch ends."""
    def_path = tmp_path / "pyproject.toml"
    _ = def_path.write_text("[project]\n", encoding="utf-8")
    raw: dict[str, Any] = {"tool": {"finecode": {}}}
    provider = _CountingProjectInfoProvider(raw, def_path)
    command_runner = _DumpSeeingCommandRunner()

    result = await _run_batch(
        tmp_path,
        3,
        command_runner=command_runner,
        provider=provider,
        dump_handler_classes=[DumpConfigHandler, DumpConfigSaveHandler],
    )

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [def_path]
    assert provider.selection_calls == 1
    venv_commands = [cmd for cmd in command_runner.commands if "venv" in cmd]
    assert len(venv_commands) == 3
    assert len(command_runner.cwds) == 3
    first = command_runner.cwds[0]
    assert first is not None
    assert all(cwd == first for cwd in command_runner.cwds)
    assert first.name == tmp_path.name
    assert first.parent.name.startswith("finecode_config_dump_")
    assert all(command_runner.saw_pyproject)
    assert not first.parent.exists()


@dataclasses.dataclass
class _MarkerHandlerConfig(code_action.ActionHandlerConfig): ...


class _DumpMarkerHandler(
    code_action.ActionHandler[dump_config_action.DumpConfigAction, _MarkerHandlerConfig]
):
    """Test-only `dump_config` middle handler: adds a marker table to the
    rendered content so consumers can prove they ran on customized output."""

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        assert run_context.config_dump_content is not None
        run_context.config_dump_content += (
            "\n[tool-shared-dump-marker]\npresent = true\n"
        )
        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )


def _shared_dump_dirs() -> set[str]:
    return {
        p.name
        for p in pathlib.Path(tempfile.gettempdir()).iterdir()
        if p.name.startswith("finecode_config_dump_")
    }


async def test_dump_without_save_reports_per_env_errors(tmp_path: pathlib.Path) -> None:
    """A `dump_config` pipeline that renders but never saves still prepares
    successfully: no uv run starts, and each env reports one error naming
    its venv and the lent file, while the shared directory is removed."""
    def_path = tmp_path / "pyproject.toml"
    _ = def_path.write_text("[project]\n", encoding="utf-8")
    raw: dict[str, Any] = {"tool": {"finecode": {}}}
    provider = _CountingProjectInfoProvider(raw, def_path)
    command_runner = _DumpSeeingCommandRunner()
    before = _shared_dump_dirs()

    result = await _run_batch(
        tmp_path,
        3,
        command_runner=command_runner,
        provider=provider,
        dump_handler_classes=[DumpConfigHandler],
    )

    assert result is not None
    assert len(result.errors) == 3
    uris: set[str] = set()
    for i in range(3):
        venv = tmp_path / "venvs" / f"env-{i}"
        matches = [error for error in result.errors if str(venv) in error]
        assert len(matches) == 1
        assert "uv was not started" in matches[0]
        start = matches[0].index("file://")
        uris.add(matches[0][start:].split()[0])
    assert len(uris) == 1
    only = next(iter(uris))
    assert only.endswith("pyproject.toml")
    assert not any("venv" in cmd for cmd in command_runner.commands)
    assert provider.raw_calls == [def_path]
    assert provider.selection_calls == 1
    assert _shared_dump_dirs() == before


async def test_customized_dump_reaches_every_uv_run(tmp_path: pathlib.Path) -> None:
    """A `dump_config` pipeline customized with an extra handler feeds every
    uv run: each uv working directory contains the marker when uv runs."""
    def_path = tmp_path / "pyproject.toml"
    _ = def_path.write_text("[project]\n", encoding="utf-8")
    raw: dict[str, Any] = {"tool": {"finecode": {}}}
    provider = _CountingProjectInfoProvider(raw, def_path)
    command_runner = _DumpSeeingCommandRunner()

    result = await _run_batch(
        tmp_path,
        3,
        command_runner=command_runner,
        provider=provider,
        dump_handler_classes=[
            DumpConfigHandler,
            _DumpMarkerHandler,
            DumpConfigSaveHandler,
        ],
    )

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [def_path]
    assert provider.selection_calls == 1
    venv_commands = [cmd for cmd in command_runner.commands if "venv" in cmd]
    assert len(venv_commands) == 3
    for cwd in command_runner.cwds:
        assert cwd is not None
    assert all(command_runner.saw_pyproject)
    assert all(command_runner.marker_seen)
