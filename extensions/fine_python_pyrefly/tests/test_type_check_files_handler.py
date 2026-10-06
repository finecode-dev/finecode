"""Tests for PyreflyTypeCheckFilesHandler's run orchestration.

The LSP service is a stub: these tests exercise how the handler drives it —
what it calls, in what order, and how the answers shape the run — not what
pyrefly itself does.
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fine_python_lang.type_check_python_files_action import TypeCheckPythonFilesAction
from fine_type_check.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticSeverity,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    isrcartifactfileclassifier,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner._services.run_action import (
    ActionFailedException as ActionRunFailed,
)
from finecode_extension_runner.testing import (
    NoOpLogger,
    Session,
    handler_test_session,
    nonexistent_abs_path,
)

from fine_python_pyrefly._error_config import render_lsp_config
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly.type_check_files_handler import (
    PyreflyTypeCheckFilesHandler,
    PyreflyTypeCheckFilesHandlerConfig,
    map_pyrefly_error_to_diagnostic,
)

_ACTION_NAME = TypeCheckPythonFilesAction.__name__
_ACTION_SOURCE = (
    f"{TypeCheckPythonFilesAction.__module__}.{TypeCheckPythonFilesAction.__qualname__}"
)
_HANDLER_NAME = PyreflyTypeCheckFilesHandler.__name__
_HANDLER_SOURCE = f"{PyreflyTypeCheckFilesHandler.__module__}.{PyreflyTypeCheckFilesHandler.__qualname__}"


class _StubPyreflyLspService:
    """Records the handler's calls to the LSP service instead of running one."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.settings_updates: list[dict[str, object]] = []
        self.ensure_started_roots: list[str] = []
        self.sweep_args: list[tuple[list[Path], float]] = []
        self.checked: list[Path] = []
        # What sync_watched_files reports as missing; tests set this to drive
        # scheduling (AC-12).
        self.missing: set[Path] = set()

    def update_settings(self, settings: dict[str, object]) -> None:
        self.calls.append("update_settings")
        self.settings_updates.append(settings)

    async def ensure_started(self, root_uri: str) -> None:
        self.calls.append("ensure_started")
        self.ensure_started_roots.append(root_uri)

    async def sync_watched_files(
        self, file_paths: list[Path], recheck_timeout: float
    ) -> set[Path]:
        self.calls.append("sync_watched_files")
        self.sweep_args.append((list(file_paths), recheck_timeout))
        return set(self.missing)

    async def check_file(self, _file_path: Path) -> list[object]:
        self.calls.append("check_file")
        self.checked.append(_file_path)
        return []


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
    """ICommandRunner-shaped fake answering per pyrefly subcommand.

    ``processes_by_subcommand`` keys on the command's second argv element
    (``check``, ``dump-config``); a subcommand without an entry behaves like
    the historical check fake and reports no errors.
    """

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


class _FakeSrcArtifactFileClassifier:
    def get_src_artifact_file_type(
        self, file_path: Path
    ) -> isrcartifactfileclassifier.SrcArtifactFileType:
        if file_path.suffix != ".py":
            return isrcartifactfileclassifier.SrcArtifactFileType.UNKNOWN
        return isrcartifactfileclassifier.SrcArtifactFileType.SOURCE

    def get_env_for_file_type(
        self, file_type: isrcartifactfileclassifier.SrcArtifactFileType
    ) -> str:
        return file_type.name.lower()


class _FakeExtensionRunnerInfoProvider:
    def __init__(self, cache_dir_path: Path | None = None) -> None:
        self._cache_dir_path = cache_dir_path or nonexistent_abs_path("fake", "cache")

    def get_cache_dir_path(self) -> Path:
        return self._cache_dir_path

    def get_venv_dir_path_of_env(self, env_name: str) -> Path:
        return nonexistent_abs_path("fake", "venvs", env_name)

    def get_venv_site_packages(self, venv_dir_path: Path) -> list[Path]:
        return [venv_dir_path / "lib" / "site-packages"]

    def get_venv_python_interpreter(self, venv_dir_path: Path) -> Path:
        return venv_dir_path / "bin" / "python"


def _overrides(
    lsp: _StubPyreflyLspService,
    *,
    command_runner: _FakeCommandRunner | None = None,
    info_provider: _FakeExtensionRunnerInfoProvider | None = None,
) -> dict[type, object]:
    return {
        PyreflyLspService: lsp,
        ilogger.ILogger: NoOpLogger(),
        icommandrunner.ICommandRunner: command_runner or _FakeCommandRunner(),
        isrcartifactfileclassifier.ISrcArtifactFileClassifier: (
            _FakeSrcArtifactFileClassifier()
        ),
        iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider: (
            info_provider or _FakeExtensionRunnerInfoProvider()
        ),
    }


def _actions(*, handler_config: dict[str, Any] | None = None) -> dict[str, dict]:
    return {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": [
                {
                    "name": _HANDLER_NAME,
                    "source": _HANDLER_SOURCE,
                    "config": handler_config or {},
                }
            ],
        }
    }


@asynccontextmanager
async def _session(
    tmp_path: Path,
    lsp: _StubPyreflyLspService,
    *,
    handler_config: dict[str, Any] | None = None,
    command_runner: _FakeCommandRunner | None = None,
    info_provider: _FakeExtensionRunnerInfoProvider | None = None,
    service_config_overrides: dict[str, dict] | None = None,
) -> AsyncGenerator[Session, None]:
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(handler_config=handler_config),
        service_overrides=_overrides(
            lsp, command_runner=command_runner, info_provider=info_provider
        ),
        service_config_overrides=service_config_overrides,
    ) as session:
        yield session


def _run_payload(paths: list[Path]) -> DiagnosticFilesRunPayload:
    return DiagnosticFilesRunPayload(
        file_paths=[path_to_resource_uri(p) for p in paths]
    )


async def test_every_run_checks_every_file_without_a_cache(
    tmp_path: Path,
) -> None:
    """A second run over the same files must check both of them again.

    A whole-program result is not cached under a single-file key (ADR-0092):
    the checked file's own bytes say nothing about whether a dependency on
    disk changed, so a run that skipped checking would replay the previous
    run's answer forever against a warm shared server.
    """
    a = (tmp_path / "a.py").resolve()
    b = (tmp_path / "b.py").resolve()

    lsp = _StubPyreflyLspService()
    async with _session(tmp_path, lsp) as session:
        payload = _run_payload([a, b])
        await session.run_action(_ACTION_NAME, payload)
        await session.run_action(_ACTION_NAME, payload)

    assert lsp.checked == [a, b, a, b]
    # Dropping the per-file cache means the handler no longer accepts one.
    parameters = inspect.signature(PyreflyTypeCheckFilesHandler.__init__).parameters
    assert "cache" not in parameters


async def test_cli_mode_skips_the_lsp_service_entirely(
    tmp_path: Path,
) -> None:
    """With use_cli the run must neither start the LSP service nor sweep
    watched files -- it checks with one ``pyrefly check`` command per file,
    which is what CI and other non-server callers use."""
    a = (tmp_path / "a.py").resolve()
    b = (tmp_path / "b.py").resolve()

    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    overrides = _overrides(lsp)
    overrides[icommandrunner.ICommandRunner] = runner
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(handler_config={"use_cli": True}),
        service_overrides=overrides,
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([a, b]))

    assert lsp.calls == []
    assert len(runner.commands) == 2
    assert all(cmd[1] == "check" for cmd in runner.commands)
    assert [Path(cmd[-1]) for cmd in runner.commands] == [a, b]
    # the interpreter path reaches pyrefly as one verbatim token -- no shell
    # quoting around it, so a path with spaces survives
    fake_info = _FakeExtensionRunnerInfoProvider()
    expected = (
        "--python-interpreter-path="
        f"{fake_info.get_venv_python_interpreter(fake_info.get_venv_dir_path_of_env('source'))}"
    )
    assert all(expected in cmd for cmd in runner.commands)


async def test_run_sweeps_the_full_file_set_before_checking(
    tmp_path: Path,
) -> None:
    """A run must start the server, sweep the whole run file set, and only
    then check files -- a per-file sync before the recheck lands is answered
    from the pre-recheck state."""
    a = (tmp_path / "a.py").resolve()
    b = (tmp_path / "b.py").resolve()

    lsp = _StubPyreflyLspService()
    async with _session(
        tmp_path, lsp, handler_config={"recheck_barrier_sec": 0.5}
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([a, b]))

    assert lsp.calls.count("sync_watched_files") == 1
    assert lsp.sweep_args == [([a, b], 0.5)]
    ensure_index = lsp.calls.index("ensure_started")
    sync_index = lsp.calls.index("sync_watched_files")
    assert ensure_index < sync_index
    check_indices = [i for i, call in enumerate(lsp.calls) if call == "check_file"]
    assert len(check_indices) == 2
    assert all(i > sync_index for i in check_indices)
    assert set(lsp.checked) == {a, b}


async def test_missing_paths_are_dropped_from_the_run(
    tmp_path: Path,
) -> None:
    """A path the sweep reports missing must not be scheduled for a check -- a
    check of it would raise FileNotFound and cancel every other file's check
    in the same run."""
    a = (tmp_path / "a.py").resolve()
    b = (tmp_path / "b.py").resolve()

    lsp = _StubPyreflyLspService()
    lsp.missing = {b}
    async with _session(tmp_path, lsp) as session:
        result = await session.run_action(_ACTION_NAME, _run_payload([a, b]))

    assert lsp.checked == [a]
    assert result is not None


@pytest.mark.parametrize("bad_value", ["loud", True])
async def test_invalid_error_severity_names_the_offending_kind(
    tmp_path: Path, bad_value: object
) -> None:
    """A severity outside pyrefly's four strings must fail at config time and
    name the kind: a lenient fallback would either hide a diagnostic or turn a
    warning into a failing error."""
    lsp = _StubPyreflyLspService()
    async with _session(
        tmp_path,
        lsp,
        handler_config={"use_cli": True},
        service_config_overrides={"pyrefly_config": {"errors": {"x": bad_value}}},
    ) as session:
        with pytest.raises(ActionRunFailed) as exc_info:
            await session.run_action(_ACTION_NAME, _run_payload([]))

    assert "$.errors['x']" in exc_info.value.message


async def test_cli_mode_without_errors_keeps_the_command_unchanged(
    tmp_path: Path,
) -> None:
    """With ``errors`` absent the command must be exactly what it was before
    the field existed: a spurious severity flag would change which diagnostics
    the run reports for every existing user."""
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    async with _session(
        tmp_path, lsp, handler_config={"use_cli": True}, command_runner=runner
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    info_provider = _FakeExtensionRunnerInfoProvider()
    venv_dir = info_provider.get_venv_dir_path_of_env("source")
    assert runner.commands == [
        [
            str(Path(sys.executable).parent / "pyrefly"),
            "check",
            "--output-format=json",
            (
                "--python-interpreter-path="
                f"{info_provider.get_venv_python_interpreter(venv_dir)}"
            ),
            f"--site-package-path={info_provider.get_venv_site_packages(venv_dir)[0]}",
            str(file_path),
        ]
    ]


async def test_cli_mode_orders_severity_flags_between_version_and_site_packages(
    tmp_path: Path,
) -> None:
    """The severity flags must sit at a fixed place in the argv, with
    ``--min-severity`` lowered whenever a configured kind would otherwise stay
    hidden below pyrefly's default error floor."""
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    handler_config = {
        "use_cli": True,
        "python_version": "3.12",
    }
    async with _session(
        tmp_path,
        lsp,
        handler_config=handler_config,
        command_runner=runner,
        service_config_overrides={
            "pyrefly_config": {
                "errors": {"a": "error", "c": "warn", "b": "warn", "d": "ignore"}
            }
        },
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    cmd = next(command for command in runner.commands if command[1] == "check")
    version_index = cmd.index("--python-version=3.12")
    site_index = next(
        index for index, arg in enumerate(cmd) if arg.startswith("--site-package-path=")
    )
    assert cmd[version_index + 1 : site_index] == [
        "--error=a",
        "--warn=b,c",
        "--ignore=d",
        "--min-severity=warn",
    ]


async def test_cli_mode_omits_min_severity_for_error_and_ignore_only(
    tmp_path: Path,
) -> None:
    """Lowering the floor is needed only for warn/info entries; with neither
    set, the default error floor already shows everything configured."""
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    handler_config = {"use_cli": True}
    async with _session(
        tmp_path,
        lsp,
        handler_config=handler_config,
        command_runner=runner,
        service_config_overrides={
            "pyrefly_config": {"errors": {"a": "error", "d": "ignore"}}
        },
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    check_command = next(
        command for command in runner.commands if command[1] == "check"
    )
    assert "--error=a" in check_command
    assert "--ignore=d" in check_command
    assert not any(arg.startswith("--min-severity") for arg in check_command)


def test_pyrefly_severity_maps_to_the_diagnostic_severity() -> None:
    """A warn-level pyrefly entry must not surface as an error: the severity
    is what the IDE renders differently and what a future non-failing warning
    path would key on."""
    base_error = {"line": 1, "column": 1, "stop_line": 1, "stop_column": 2}

    assert (
        map_pyrefly_error_to_diagnostic({**base_error, "severity": "warn"}).severity
        is DiagnosticSeverity.WARNING
    )
    assert (
        map_pyrefly_error_to_diagnostic({**base_error, "severity": "info"}).severity
        is DiagnosticSeverity.INFO
    )
    assert (
        map_pyrefly_error_to_diagnostic({**base_error, "severity": "error"}).severity
        is DiagnosticSeverity.ERROR
    )
    assert (
        map_pyrefly_error_to_diagnostic(base_error).severity is DiagnosticSeverity.ERROR
    )


async def test_non_json_output_reports_pyrefly_stderr(tmp_path: Path) -> None:
    """An unknown kind makes ``pyrefly check`` print usage text on stderr and
    nothing on stdout; without the stderr in the failure message, the operator
    sees an empty payload and nothing that names the bad kind."""
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner(
        {
            "check": _FakePyreflyProcess(
                output="",
                error_output="error: invalid value 'bogus-kind'",
                exit_code=2,
            )
        }
    )
    handler_config = {"use_cli": True}
    async with _session(
        tmp_path,
        lsp,
        handler_config=handler_config,
        command_runner=runner,
        service_config_overrides={"pyrefly_config": {"errors": {"bogus-kind": "warn"}}},
    ) as session:
        with pytest.raises(ActionRunFailed) as exc_info:
            await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    assert "bogus-kind" in exc_info.value.message


@pytest.mark.parametrize("use_cli", [True, False])
async def test_project_pyrefly_config_with_service_errors_fails_in_both_modes(
    tmp_path: Path, use_cli: bool
) -> None:
    """A project pyrefly config and configured errors cannot both apply:
    pyrefly would silently ignore one of them, so the run has to name the file
    that must be removed instead of letting the effect depend on which
    discovery path wins."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config_path = project_dir / "pyrefly.toml"
    config_path.write_text(
        "disable-project-excludes-heuristics = true\n", encoding="utf-8"
    )

    lsp = _StubPyreflyLspService()
    async with _session(
        project_dir,
        lsp,
        handler_config={"use_cli": use_cli},
        service_config_overrides={
            "pyrefly_config": {"errors": {"bad-assignment": "warn"}}
        },
        info_provider=_FakeExtensionRunnerInfoProvider(tmp_path / "cache"),
    ) as session:
        with pytest.raises(code_action.ActionFailedException) as exc_info:
            await session.run_action(
                _ACTION_NAME, _run_payload([(project_dir / "a.py").resolve()])
            )

    assert str(config_path) in exc_info.value.message


@pytest.mark.parametrize("use_cli", [True, False])
async def test_plain_pyproject_with_service_errors_does_not_fail(
    tmp_path: Path, use_cli: bool
) -> None:
    """A ``pyproject.toml`` without ``[tool.pyrefly]`` is only a project-root
    marker; refusing on it would break every project that has one."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    lsp = _StubPyreflyLspService()
    async with _session(
        project_dir,
        lsp,
        handler_config={"use_cli": use_cli},
        service_config_overrides={
            "pyrefly_config": {"errors": {"bad-assignment": "warn"}}
        },
        info_provider=_FakeExtensionRunnerInfoProvider(tmp_path / "cache"),
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, _run_payload([(project_dir / "a.py").resolve()])
        )

    assert result is not None


@pytest.mark.parametrize("use_cli", [True, False])
async def test_project_pyrefly_config_without_service_errors_does_not_fail(
    tmp_path: Path, use_cli: bool
) -> None:
    """A user who has not opted into configured errors keeps the project
    pyrefly config they already rely on."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyrefly.toml").write_text(
        "disable-project-excludes-heuristics = true\n", encoding="utf-8"
    )

    lsp = _StubPyreflyLspService()
    async with _session(
        project_dir,
        lsp,
        handler_config={"use_cli": use_cli},
        info_provider=_FakeExtensionRunnerInfoProvider(tmp_path / "cache"),
    ) as session:
        result = await session.run_action(
            _ACTION_NAME, _run_payload([(project_dir / "a.py").resolve()])
        )

    assert result is not None


async def test_lsp_mode_without_errors_writes_no_config(tmp_path: Path) -> None:
    """With ``errors`` absent the handler must not take over pyrefly's config:
    a generated file would shadow any pyrefly.toml the project already has."""
    cache_dir = tmp_path / "cache"
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    async with _session(
        tmp_path,
        lsp,
        command_runner=runner,
        info_provider=_FakeExtensionRunnerInfoProvider(cache_dir),
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    assert runner.commands == []
    assert lsp.settings_updates == [{"pyrefly": {"displayTypeErrors": "force-on"}}]
    assert not cache_dir.exists()


async def test_lsp_mode_validates_the_generated_config_once_across_runs(
    tmp_path: Path,
) -> None:
    """Configured errors are a property of the runner's service, not of a
    handler instance: two runs must pay the validation once. Rewriting or
    revalidating on every run would pay a subprocess for a file whose content
    is already pinned by the service config."""
    cache_dir = tmp_path / "cache"
    config_path = cache_dir / "pyrefly" / "pyrefly.toml"
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    async with _session(
        tmp_path,
        lsp,
        service_config_overrides={
            "pyrefly_config": {"errors": {"implicit-any-type-argument": "warn"}}
        },
        command_runner=runner,
        info_provider=_FakeExtensionRunnerInfoProvider(cache_dir),
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    assert config_path.read_text(encoding="utf-8") == render_lsp_config(
        tmp_path, {"implicit-any-type-argument": "warn"}
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


async def test_lsp_mode_invalid_generated_config_fails_before_start(
    tmp_path: Path,
) -> None:
    """pyrefly's LSP ignores an invalid config file wholesale and reports no
    error, so a config it would reject has to fail the run instead: otherwise
    every configured kind silently stops applying."""
    cache_dir = tmp_path / "cache"
    config_path = cache_dir / "pyrefly" / "pyrefly.toml"
    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner(
        {
            "dump-config": _FakePyreflyProcess(
                output="Fatal configuration error",
                exit_code=1,
            )
        }
    )
    async with _session(
        tmp_path,
        lsp,
        service_config_overrides={
            "pyrefly_config": {"errors": {"implicit-any-type-argument": "warn"}}
        },
        command_runner=runner,
        info_provider=_FakeExtensionRunnerInfoProvider(cache_dir),
    ) as session:
        with pytest.raises(code_action.ActionFailedException) as exc_info:
            await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    assert "Fatal configuration error" in exc_info.value.message
    assert lsp.calls == []
    assert str(config_path) in exc_info.value.message


async def test_handler_errors_field_is_inert(tmp_path: Path) -> None:
    """The old per-handler ``errors`` surface must not half-work: the
    handler-config converter silently drops unknown keys, so a config still
    carrying ``errors`` produces no severity flags at all. A half-applied
    severity table would be worse than an inert one."""
    assert "errors" not in {
        field.name for field in dataclasses.fields(PyreflyTypeCheckFilesHandlerConfig)
    }

    file_path = (tmp_path / "a.py").resolve()
    lsp = _StubPyreflyLspService()
    runner = _FakeCommandRunner()
    async with _session(
        tmp_path,
        lsp,
        handler_config={"use_cli": True, "errors": {"a": "warn"}},
        command_runner=runner,
    ) as session:
        await session.run_action(_ACTION_NAME, _run_payload([file_path]))

    check_command = next(
        command for command in runner.commands if command[1] == "check"
    )
    assert not any(arg.startswith("--warn=") for arg in check_command)
    assert not any(arg.startswith("--min-severity") for arg in check_command)
