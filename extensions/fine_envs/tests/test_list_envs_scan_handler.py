import pathlib
from typing import Any

from finecode_extension_api import code_action

from fine_envs import list_envs_action
from fine_envs.env_inventory import EnvState
from fine_envs.list_envs_scan_handler import ListEnvsScanHandler


class _FakeProjectInfoProvider:
    def __init__(self, raw_config: dict[str, Any]) -> None:
        self._raw_config = raw_config

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        return self._raw_config


class _FakeExtensionRunnerInfoProvider:
    def __init__(self, project_dir: pathlib.Path) -> None:
        self._project_dir = project_dir

    def get_current_venv_dir_path(self) -> pathlib.Path:
        return self._project_dir / ".venvs" / "dev_workspace"


class _FakeLogger:
    def debug(self, message: str) -> None: ...


def _make_run_context() -> list_envs_action.ListEnvsRunContext:
    return list_envs_action.ListEnvsRunContext(
        run_id=1,
        initial_payload=list_envs_action.ListEnvsRunPayload(),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )


def _make_venv(venvs_dir: pathlib.Path, name: str) -> None:
    venv_dir = venvs_dir / name
    venv_dir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")


async def test_matrix_base_venv_reported_as_orphan(tmp_path: pathlib.Path) -> None:
    """`testing` was converted to a matrix env, so the resolved config declares
    only its concrete children (ADR-0047 expansion) and `.venvs/testing` is
    left behind referenced by nothing."""
    venvs_dir = tmp_path / ".venvs"
    for name in (
        "dev_workspace",
        "testing",
        "testing@cpython-3.11",
        "testing@cpython-3.12",
    ):
        _make_venv(venvs_dir, name)

    raw_config = {
        "dependency-groups": {
            "dev_workspace": [],
            "testing@cpython-3.11": [],
            "testing@cpython-3.12": [],
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
    handler = ListEnvsScanHandler(
        project_info_provider=_FakeProjectInfoProvider(raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )

    result = await handler.run(
        list_envs_action.ListEnvsRunPayload(), _make_run_context()
    )

    assert [env.name for env in result.envs if env.orphaned] == ["testing"]
    assert all(env.state is EnvState.CREATED for env in result.envs)


async def test_declared_env_without_venv_is_missing(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")

    raw_config = {
        "dependency-groups": {"dev_workspace": [], "testing@cpython-3.14": []},
    }
    handler = ListEnvsScanHandler(
        project_info_provider=_FakeProjectInfoProvider(raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )

    result = await handler.run(
        list_envs_action.ListEnvsRunPayload(), _make_run_context()
    )

    by_name = {env.name: env for env in result.envs}
    assert by_name["testing@cpython-3.14"].state is EnvState.MISSING
    assert by_name["testing@cpython-3.14"].orphaned is False


async def test_broken_venv_reported(tmp_path: pathlib.Path) -> None:
    venvs_dir = tmp_path / ".venvs"
    _make_venv(venvs_dir, "dev_workspace")
    (venvs_dir / "dev").mkdir()

    raw_config = {"dependency-groups": {"dev_workspace": [], "dev": []}}
    handler = ListEnvsScanHandler(
        project_info_provider=_FakeProjectInfoProvider(raw_config),
        runner_info_provider=_FakeExtensionRunnerInfoProvider(tmp_path),
        logger=_FakeLogger(),
    )

    result = await handler.run(
        list_envs_action.ListEnvsRunPayload(), _make_run_context()
    )

    by_name = {env.name: env for env in result.envs}
    assert by_name["dev"].state is EnvState.BROKEN
    assert by_name["dev"].orphaned is False
