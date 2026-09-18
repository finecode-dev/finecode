"""Tests for PyreflyTypeCheckFilesHandler's run orchestration.

The LSP service is a stub: these tests exercise how the handler drives it —
what it calls, in what order, and how the answers shape the run — not what
pyrefly itself does.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fine_python_lang.type_check_python_files_action import TypeCheckPythonFilesAction
from fine_type_check.diagnostic_types import DiagnosticFilesRunPayload
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    isrcartifactfileclassifier,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import NoOpLogger, Session, handler_test_session

from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly.type_check_files_handler import PyreflyTypeCheckFilesHandler

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
        self.ensure_started_roots: list[str] = []
        self.sweep_args: list[tuple[list[Path], float]] = []
        self.checked: list[Path] = []
        # What sync_watched_files reports as missing; tests set this to drive
        # scheduling (AC-12).
        self.missing: set[Path] = set()

    def update_settings(self, _settings: dict[str, object]) -> None:
        self.calls.append("update_settings")

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
    def get_output(self) -> str:
        return '{"errors": []}'

    async def wait_for_end(self, _timeout: float | None = None) -> None:
        return None


class _FakeCommandRunner:
    """ICommandRunner-shaped fake whose processes always report no errors."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(
        self,
        cmd: str,
        _cwd: Path | None = None,
        _env: dict[str, str] | None = None,
        _new_process_group: bool = False,
    ) -> _FakePyreflyProcess:
        self.commands.append(cmd)
        return _FakePyreflyProcess()


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
    def get_venv_dir_path_of_env(self, env_name: str) -> Path:
        return Path("/fake") / "venvs" / env_name

    def get_venv_site_packages(self, venv_dir_path: Path) -> list[Path]:
        return [venv_dir_path / "lib" / "site-packages"]

    def get_venv_python_interpreter(self, venv_dir_path: Path) -> Path:
        return venv_dir_path / "bin" / "python"


def _overrides(lsp: _StubPyreflyLspService) -> dict[type, object]:
    return {
        PyreflyLspService: lsp,
        ilogger.ILogger: NoOpLogger(),
        icommandrunner.ICommandRunner: _FakeCommandRunner(),
        isrcartifactfileclassifier.ISrcArtifactFileClassifier: (
            _FakeSrcArtifactFileClassifier()
        ),
        iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider: (
            _FakeExtensionRunnerInfoProvider()
        ),
    }


def _actions(*, handler_config: dict[str, object] | None = None) -> dict[str, dict]:
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
    handler_config: dict[str, object] | None = None,
) -> AsyncGenerator[Session, None]:
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(handler_config=handler_config),
        service_overrides=_overrides(lsp),
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
    assert all("pyrefly check" in cmd for cmd in runner.commands)
    assert [Path(cmd.rsplit(" ", 1)[1]) for cmd in runner.commands] == [a, b]


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
