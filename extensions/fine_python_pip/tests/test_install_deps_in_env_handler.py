import pathlib

from fine_envs import install_deps_in_env_action
from fine_python_pip.install_deps_in_env_handler import (
    PipInstallDepsInEnvHandler,
    PipInstallDepsInEnvHandlerConfig,
)


def _handler() -> PipInstallDepsInEnvHandler:
    return PipInstallDepsInEnvHandler(
        config=PipInstallDepsInEnvHandlerConfig(),
        command_runner=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
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


def test_pip_editable_dep_emits_extras() -> None:
    """An editable spec with extras appends the bracket group to the file URI."""
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[
            _dep("pkg", " @ file:///tmp/pkg", editable=True, extras=["a"])
        ],
    )

    assert "file:///tmp/pkg[a]" in cmd


def test_pip_non_editable_dep_emits_extras() -> None:
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[_dep("pkg", "~=1.0", extras=["a"])],
    )

    assert "'pkg[a]~=1.0'" in cmd
