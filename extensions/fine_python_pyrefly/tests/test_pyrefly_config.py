"""Tests for ``PyreflyConfig``: the check for a conflicting project config, the
generated LSP config file, its validation, and the CLI flags.

The service is exercised directly with fakes; the handler tests cover what the
consumers do with it.
"""

from __future__ import annotations

import sys
import typing
from pathlib import Path

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_runner.testing import NoOpLogger, nonexistent_abs_path

from fine_python_pyrefly._error_config import PyreflyErrorSeverity, render_lsp_config
from fine_python_pyrefly.pyrefly_config import PyreflyConfig, PyreflySettings


class _FakePyreflyProcess:
    def __init__(
        self,
        *,
        output: str = '{"errors": []}',
        error_output: str = "",
        exit_code: int = 0,
    ) -> None:
        self._output = output
        self._error_output = error_output
        self._exit_code = exit_code

    def get_output(self) -> str:
        return self._output

    def get_error_output(self) -> str:
        return self._error_output

    def get_exit_code(self) -> int:
        return self._exit_code

    async def wait_for_end(self, _timeout: float | None = None) -> None:
        return None


class _FakeCommandRunner:
    """ICommandRunner-shaped fake answering per pyrefly subcommand."""

    def __init__(
        self, processes_by_subcommand: dict[str, _FakePyreflyProcess] | None = None
    ) -> None:
        self.commands: list[list[str]] = []
        self._processes_by_subcommand = processes_by_subcommand or {}

    async def run(
        self,
        cmd: icommandrunner.Argv,
        _cwd: Path | None = None,
        _env: dict[str, str] | None = None,
        _new_process_group: bool = False,
    ) -> _FakePyreflyProcess:
        icommandrunner.check_argv(cmd)
        self.commands.append(list(cmd))
        return self._processes_by_subcommand.get(cmd[1], _FakePyreflyProcess())


class _FakeExtensionRunnerInfoProvider:
    def __init__(self, cache_dir_path: Path | None = None) -> None:
        self._cache_dir_path = cache_dir_path or nonexistent_abs_path("fake", "cache")

    def get_cache_dir_path(self) -> Path:
        return self._cache_dir_path


class _FakeProjectInfoProvider:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> Path:
        return self._project_dir


def _service(
    project_dir: Path,
    *,
    errors: dict[str, PyreflyErrorSeverity] | None = None,
    command_runner: _FakeCommandRunner | None = None,
    cache_dir_path: Path | None = None,
) -> tuple[PyreflyConfig, _FakeCommandRunner]:
    runner = command_runner or _FakeCommandRunner()
    config = PyreflyConfig(
        config=PyreflySettings(errors=errors or {}),
        command_runner=typing.cast(icommandrunner.ICommandRunner, runner),
        extension_runner_info_provider=typing.cast(
            typing.Any, _FakeExtensionRunnerInfoProvider(cache_dir_path)
        ),
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(project_dir)
        ),
        logger=NoOpLogger(),
    )
    return config, runner


async def test_empty_errors_touches_nothing(tmp_path: Path) -> None:
    """Without configured errors the service must leave pyrefly's own config
    discovery and the cache directory alone: writing or validating a generated
    file would shadow a project pyrefly.toml the user relies on."""
    cache_dir = tmp_path / "cache"
    config, runner = _service(tmp_path, cache_dir_path=cache_dir)

    await config.init()

    assert config.lsp_config_path is None
    assert runner.commands == []
    assert not cache_dir.exists()
    assert config.cli_args() == []


async def test_errors_writes_and_validates_the_generated_config(
    tmp_path: Path,
) -> None:
    """The generated file is what makes the configured severities apply in LSP
    mode, and validating it here is what turns a config pyrefly would reject
    wholesale into a readable failure."""
    errors: dict[str, PyreflyErrorSeverity] = {"implicit-any-type-argument": "warn"}
    cache_dir = tmp_path / "cache"
    config_path = cache_dir / "pyrefly" / "pyrefly.toml"
    config, runner = _service(tmp_path, errors=errors, cache_dir_path=cache_dir)

    await config.init()

    assert config_path.read_text(encoding="utf-8") == render_lsp_config(
        tmp_path, errors
    )
    assert runner.commands == [
        [
            str(Path(sys.executable).parent / "pyrefly"),
            "dump-config",
            "-c",
            str(config_path),
            str(config_path),
        ]
    ]
    assert config.lsp_config_path == config_path
    assert config.cli_args() == [
        "--warn=implicit-any-type-argument",
        "--min-severity=warn",
    ]


async def test_invalid_generated_config_fails_and_leaves_no_path(
    tmp_path: Path,
) -> None:
    """An invalid generated config must fail before any consumer can use it:
    pyrefly's LSP ignores such a file and reports no error, so every configured
    kind would silently stop applying."""
    cache_dir = tmp_path / "cache"
    runner = _FakeCommandRunner(
        {
            "dump-config": _FakePyreflyProcess(
                output="Fatal configuration error",
                exit_code=1,
            )
        }
    )
    config, _ = _service(
        tmp_path,
        errors={"implicit-any-type-argument": "warn"},
        command_runner=runner,
        cache_dir_path=cache_dir,
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await config.init()

    assert "Fatal configuration error" in exc_info.value.message
    assert config.lsp_config_path is None


@pytest.mark.parametrize(
    ("config_name", "config_text", "in_parent"),
    [
        ("pyrefly.toml", "disable-project-excludes-heuristics = true\n", False),
        ("pyrefly.toml", "disable-project-excludes-heuristics = true\n", True),
        ("pyproject.toml", "[tool.pyrefly]\n", False),
    ],
)
async def test_conflicting_project_config_fails_without_running_pyrefly(
    tmp_path: Path,
    config_name: str,
    config_text: str,
    in_parent: bool,
) -> None:
    """A project pyrefly config and configured errors cannot both apply:
    pyrefly would silently ignore one of them, so the failure must name the
    file to remove before any subprocess is paid for."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config_path = (tmp_path if in_parent else project_dir) / config_name
    config_path.write_text(config_text, encoding="utf-8")

    config, runner = _service(
        project_dir,
        errors={"implicit-any-type-argument": "warn"},
        cache_dir_path=tmp_path / "cache",
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await config.init()

    assert str(config_path) in exc_info.value.message
    assert runner.commands == []
    assert config.lsp_config_path is None


async def test_plain_pyproject_without_tool_pyrefly_does_not_fail(
    tmp_path: Path,
) -> None:
    """A ``pyproject.toml`` without ``[tool.pyrefly]`` is only a project-root
    marker; refusing on it would break every project that has one."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    config, _ = _service(
        tmp_path,
        errors={"implicit-any-type-argument": "warn"},
        cache_dir_path=tmp_path / "cache",
    )

    await config.init()

    assert config.lsp_config_path is not None
