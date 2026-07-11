import pathlib
from typing import Any

from fine_envs import create_envs_action
from fine_envs.create_envs_discover_envs_handler import CreateEnvsDiscoverEnvsHandler
from finecode_extension_api import code_action


class _FakeProjectInfoProvider:
    def __init__(self, project_def_path: pathlib.Path, raw_config: dict[str, Any]) -> None:
        self._project_def_path = project_def_path
        self._raw_config = raw_config

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._project_def_path

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        return self._raw_config


class _FakeExtensionRunnerInfoProvider:
    def __init__(self, project_dir: pathlib.Path) -> None:
        self._project_dir = project_dir

    def get_venv_dir_path_of_env(self, env_name: str) -> pathlib.Path:
        return self._project_dir / ".venvs" / env_name


class _FakeLogger:
    def debug(self, message: str) -> None: ...


def _make_payload() -> create_envs_action.CreateEnvsRunPayload:
    return create_envs_action.CreateEnvsRunPayload(envs=None)


async def test_discovered_env_carries_interpreter_from_raw_config(
    tmp_path: pathlib.Path,
) -> None:
    """A concrete matrix env's materialized `interpreter` value must reach the
    discovered `EnvInfo` so venv creation later knows which interpreter to use.
    """
    project_def_path = tmp_path / "pyproject.toml"
    raw_config = {
        "dependency-groups": {"testing@cpython-3.11": []},
        "tool": {
            "finecode": {
                "env": {"testing@cpython-3.11": {"interpreter": "cpython@3.11"}}
            }
        },
    }
    handler = CreateEnvsDiscoverEnvsHandler(
        project_info_provider=_FakeProjectInfoProvider(project_def_path, raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=_make_payload(),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )

    await handler.run(_make_payload(), run_context)

    assert run_context.envs is not None
    assert len(run_context.envs) == 1
    env_info = run_context.envs[0]
    assert env_info.name == "testing@cpython-3.11"
    assert env_info.interpreter == "cpython@3.11"


async def test_discovered_env_without_interpreter_is_none(
    tmp_path: pathlib.Path,
) -> None:
    """A project without an interpreter axis must be unchanged: `interpreter` is
    ``None`` and venv creation must fall back to the default interpreter.
    """
    project_def_path = tmp_path / "pyproject.toml"
    raw_config = {
        "dependency-groups": {"dev": []},
    }
    handler = CreateEnvsDiscoverEnvsHandler(
        project_info_provider=_FakeProjectInfoProvider(project_def_path, raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=_make_payload(),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )

    await handler.run(_make_payload(), run_context)

    assert run_context.envs is not None
    assert len(run_context.envs) == 1
    env_info = run_context.envs[0]
    assert env_info.name == "dev"
    assert env_info.interpreter is None


async def test_env_names_filters_discovered_envs(tmp_path: pathlib.Path) -> None:
    """`payload.env_names` restricts discovery to the named envs, mirroring
    `install_envs`'s discovery filter — this is what lets `prepare-envs` skip
    creating unselected matrix children (PRD-0003 AC8).
    """
    project_def_path = tmp_path / "pyproject.toml"
    raw_config = {
        "dependency-groups": {
            "testing@cpython-3.11": [],
            "testing@cpython-3.12": [],
            "dev": [],
        },
        "tool": {
            "finecode": {
                "env": {
                    "testing@cpython-3.11": {"interpreter": "cpython@3.11"},
                    "testing@cpython-3.12": {"interpreter": "cpython@3.12"},
                }
            }
        },
    }
    handler = CreateEnvsDiscoverEnvsHandler(
        project_info_provider=_FakeProjectInfoProvider(project_def_path, raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )
    payload = create_envs_action.CreateEnvsRunPayload(
        envs=None, env_names=["testing@cpython-3.11", "dev"]
    )
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )

    await handler.run(payload, run_context)

    assert run_context.envs is not None
    assert {e.name for e in run_context.envs} == {"testing@cpython-3.11", "dev"}
