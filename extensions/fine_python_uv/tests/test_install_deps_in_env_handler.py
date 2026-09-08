import pathlib

from fine_envs import install_deps_in_env_action
from fine_python_uv.install_deps_in_env_handler import (
    UvInstallDepsInEnvHandler,
    UvInstallDepsInEnvHandlerConfig,
)


def _handler() -> UvInstallDepsInEnvHandler:
    return UvInstallDepsInEnvHandler(
        config=UvInstallDepsInEnvHandlerConfig(),
        command_runner=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
        action_runner=None,  # type: ignore[arg-type]
        project_info_provider=None,  # type: ignore[arg-type]
    )


def _dep(
    name: str, version_or_source: str, *, editable: bool = False, extras: list[str] | None = None
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
        uv_executable="uv",
        venv_dir_path=pathlib.Path("/venv"),
        dependencies=[
            _dep("pkg", " @ file:///tmp/pkg", editable=True, extras=["a"])
        ],
    )

    assert "pkg[a] @ file:///tmp/pkg" in cmd


def test_uv_non_editable_dep_emits_extras() -> None:
    cmd = _handler()._construct_uv_install_cmd(
        uv_executable="uv",
        venv_dir_path=pathlib.Path("/venv"),
        dependencies=[_dep("pkg", "~=1.0", extras=["a"])],
    )

    assert "'pkg[a]~=1.0'" in cmd
