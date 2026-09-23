import pathlib

import pytest
from fine_envs import install_deps_in_env_action

from fine_python_pip.install_deps_in_env_handler import (
    PipInstallDepsInEnvHandler,
    PipInstallDepsInEnvHandlerConfig,
)


def _handler(editable_mode: str | None = None) -> PipInstallDepsInEnvHandler:
    return PipInstallDepsInEnvHandler(
        config=PipInstallDepsInEnvHandlerConfig(editable_mode=editable_mode),
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


def test_pip_editable_dep_emits_extras() -> None:
    """An editable spec with extras appends the bracket group to the file URI."""
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[_dep("pkg", " @ file:///tmp/pkg", editable=True, extras=["a"])],
    )

    assert "file:///tmp/pkg[a]" in cmd


def test_pip_non_editable_dep_emits_extras() -> None:
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[_dep("pkg", "~=1.0", extras=["a"])],
    )

    assert "pkg[a]~=1.0" in cmd


def test_pip_cmd_argv_is_exact() -> None:
    """Each requirement is one argv element and no token carries quoting.

    The quoting this test used to assert (double-quoting requirements for
    cmd.exe) went away with the argv API: no shell parses these arguments, so
    on every platform pip receives exactly these tokens.
    """
    cmd = _handler(editable_mode="compat")._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[
            _dep("pkg", " @ file:///D:/a/pkg", editable=True, extras=["a"]),
            _dep("other", ">=1.0"),
            _dep("pkg", ' ; python_version < "3.12"'),
        ],
    )

    assert cmd == [
        "/venv/bin/python",
        "-m",
        "pip",
        "--disable-pip-version-check",
        "install",
        "--config-settings",
        "editable_mode=compat",
        "-e",
        "file:///D:/a/pkg[a]",
        "other>=1.0",
        "pkg ; python_version < \"3.12\"",
    ]


def test_pip_marker_string_survives_verbatim() -> None:
    """A PEP 508 marker string with double quotes reaches pip as one token.

    Under exec the marker quotes need no escaping at all: pip (not a shell)
    parses the marker, so `"` inside the requirement stays a marker quote.
    """
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[_dep("pkg", ' ; python_version < "3.12"')],
    )

    assert cmd[-1] == 'pkg ; python_version < "3.12"'
