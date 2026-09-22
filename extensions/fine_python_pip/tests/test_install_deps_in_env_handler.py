import pathlib
import shlex
import sys

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


def _split_windows_cmdline(cmd: str) -> list[str]:
    """Split `cmd` the way a Windows program's C runtime does: whitespace
    outside double quotes separates arguments and the quotes are removed.
    `'` is an ordinary character."""
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in cmd:
        if ch == '"':
            in_quotes = not in_quotes
        elif ch.isspace() and not in_quotes:
            if current:
                args.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current))
    return args


def _unquoted_spans(cmd: str) -> str:
    """The text of `cmd` outside double quotes — where cmd.exe still treats
    `<`, `>`, `&` and `|` as operators."""
    parts: list[str] = []
    in_quotes = False
    for ch in cmd:
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            parts.append(ch)
    return "".join(parts)


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

    assert "'pkg[a]~=1.0'" in cmd


def test_pip_cmd_tokenizes_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the shell is cmd.exe, where single quotes do not group a
    requirement token and an unquoted `>` redirects output. Double-quoting each
    requirement and the config setting keeps each one argument and keeps cmd's
    metacharacters literal."""
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = _handler(editable_mode="compat")._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[
            _dep("pkg", " @ file:///D:/a/pkg", editable=True, extras=["a"]),
            _dep("other", ">=1.0"),
        ],
    )

    argv = _split_windows_cmdline(cmd)

    assert ["--config-settings", "editable_mode=compat"] == argv[
        argv.index("--config-settings") : argv.index("--config-settings") + 2
    ]
    assert ["-e", "file:///D:/a/pkg[a]"] == argv[
        argv.index("-e") : argv.index("-e") + 2
    ]
    assert "other>=1.0" in argv
    assert not any("'" in token for token in argv)
    assert not any(ch in _unquoted_spans(cmd) for ch in "<>&|")


def test_pip_cmd_argv_unchanged_on_posix() -> None:
    """On POSIX the argv delivered to pip is the same as the single-quoted form
    produced before, so the Windows fix has no Linux/macOS regression surface."""
    cmd = _handler(editable_mode="compat")._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[
            _dep("pkg", " @ file:///D:/a/pkg", editable=True, extras=["a"]),
            _dep("other", ">=1.0"),
        ],
    )

    argv = shlex.split(cmd)

    assert ["--config-settings", "editable_mode=compat"] == argv[
        argv.index("--config-settings") : argv.index("--config-settings") + 2
    ]
    assert ["-e", "file:///D:/a/pkg[a]"] == argv[
        argv.index("-e") : argv.index("-e") + 2
    ]
    assert "other>=1.0" in argv


def test_pip_marker_quotes_survive_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PEP 508 marker string quoted with `"` reaches the installer as one
    argument on Windows: the double quote is swapped for the equivalent `'`
    marker quote rather than escaped."""
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = _handler()._construct_pip_install_cmd(
        python_executable=pathlib.Path("/venv/bin/python"),
        dependencies=[_dep("pkg", ' ; python_version < "3.12"')],
    )

    argv = _split_windows_cmdline(cmd)

    marker_tokens = [token for token in argv if "python_version" in token]
    assert marker_tokens == ["pkg ; python_version < '3.12'"]
