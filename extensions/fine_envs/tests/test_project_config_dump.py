import pathlib
from typing import Any, cast

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)

from fine_envs import create_env_action, dump_config_action, project_config_dump


class _FakeActionRunner:
    def __init__(self) -> None:
        self.recorded: list[dump_config_action.DumpConfigRunPayload] = []

    async def run_action(
        self, *, action_type: Any, payload: Any, meta: Any, caller_kwargs: Any = None
    ) -> None:
        self.recorded.append(payload)


class _FakeInfoProvider:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.calls: list[pathlib.Path] = []

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        self.calls.append(project_def_path)
        return self.raw


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )


async def test_write_makes_one_fetch_and_one_dispatch_with_exact_payload(
    tmp_path: pathlib.Path,
) -> None:
    """The shared producer fetches the raw config once and dispatches one
    unformatted dump_config write, so every consumer runs uv on identical input."""
    project_def_path = tmp_path / "pyproject.toml"
    _ = project_def_path.write_text("[project]\n", encoding="utf-8")
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    raw: dict[str, Any] = {"tool": {"finecode": {}}}
    runner = _FakeActionRunner()
    info = _FakeInfoProvider(raw)

    target_file = dump_dir / "dump.toml"

    await project_config_dump.write_project_config_dump(
        project_def_path=project_def_path,
        target_file=target_file,
        action_runner=cast(Any, runner),
        project_info_provider=cast(Any, info),
        logger=cast(Any, _NullLogger()),
        meta=_meta(),
    )

    assert info.calls == [project_def_path]
    assert len(runner.recorded) == 1
    dumped = runner.recorded[0]
    assert dumped.source_file_path == path_to_resource_uri(project_def_path)
    assert dumped.project_raw_config == raw
    assert dumped.target_file_path == path_to_resource_uri(target_file)
    assert dumped.format_output is False


class _NullLogger:
    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


def test_payload_defaults_to_no_definition() -> None:
    """A `create_env` payload without a lent file carries no definition."""
    env = create_env_action.EnvInfo if False else None  # keep import live
    _ = env
    from fine_envs.create_envs_action import EnvInfo as _EnvInfo

    payload = create_env_action.CreateEnvRunPayload(
        env=_EnvInfo(
            name="e",
            venv_dir_path=path_to_resource_uri(pathlib.Path("/tmp/venvs/e")),
            project_def_path=path_to_resource_uri(pathlib.Path("/tmp/pyproject.toml")),
        )
    )
    assert payload.resolved_project_def_path is None
