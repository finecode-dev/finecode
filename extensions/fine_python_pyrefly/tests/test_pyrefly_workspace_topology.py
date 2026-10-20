"""Tests for the pyrefly workspace topology: facade preconditions and the host.

A wrong precondition picks N servers where one is required, or refuses a valid
workspace; a wrong coalesce either pushes a folder twice or never tells the
server about a project at all. Both are silent from the editor's side.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import typing
from pathlib import Path
from unittest import mock

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iworkspaceinfoprovider
from finecode_extension_api.resource_uri import ResourceUri
from finecode_extension_runner.testing import NoOpLogger, nonexistent_abs_path

from fine_python_pyrefly import pyrefly_lsp_service as service_module
from fine_python_pyrefly.announce_pyrefly_project_action import (
    AnnouncePyreflyProjectAction,
)
from fine_python_pyrefly.check_files_on_pyrefly_host_action import (
    CheckFilesOnPyreflyHostAction,
)
from fine_python_pyrefly.host_types import (
    AnnouncePyreflyProjectPayload,
    AnnouncePyreflyProjectResult,
    CheckFilesOnPyreflyHostResult,
    FileCheckOutcome,
    FileCheckStatus,
    PyreflyProjectIdentity,
    PyreflyQueryKind,
    PyreflyTopology,
    QueryPyreflyHostPayload,
    QueryPyreflyHostResult,
)
from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService
from fine_python_pyrefly.query_pyrefly_host_action import QueryPyreflyHostAction


class _NoopWorkSlots:
    def acquire(self) -> typing.Any:
        return _NoopContext()


class _NoopContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeExtensionRunnerInfoProvider:
    def get_cache_dir_path(self) -> Path:
        return nonexistent_abs_path("fake", "cache")

    def get_current_env_name(self) -> str:
        return "dev_no_runtime"

    def get_venv_dir_path_of_env(self, env_name: str) -> Path:
        return nonexistent_abs_path("fake", "venvs", env_name)

    def get_venv_site_packages(self, venv_dir_path: Path) -> list[Path]:
        return [venv_dir_path / "lib" / "site-packages"]

    def get_venv_python_interpreter(self, venv_dir_path: Path) -> Path:
        return venv_dir_path / "bin" / "python"


class _FakePyreflyConfig:
    def __init__(self, lsp_config_path: Path, topology: str) -> None:
        self.lsp_config_path = lsp_config_path
        self.topology = topology


class _FakeProjectInfoProvider:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> Path:
        return self._project_dir


class _FakeWorkspaceInfoProvider:
    def __init__(self, root: Path | None, *, declares: bool = True) -> None:
        self.root = root
        self.declares = declares

    async def get_workspace_projects(
        self,
    ) -> list[iworkspaceinfoprovider.WorkspaceProject]:
        return []

    async def get_workspace_root(self) -> Path | None:
        return self.root

    async def project_declares_action(self, _path: Path, _action_source: str) -> bool:
        return self.declares


class _RecordingInnerLspService:
    def __init__(self) -> None:
        self.init_calls = 0
        self.update_settings_calls: list[dict[str, typing.Any]] = []
        self.added_folders: list[tuple[str, str]] = []
        self.send_settings_calls = 0
        self.sweeps: list[tuple[list[Path], float]] = []
        self.disposed = 0
        self.has_open_documents = False
        self.resolver: typing.Any = None
        self.ensure_started_calls = 0

    async def init(self) -> None:
        self.init_calls += 1

    def update_settings(self, settings: dict[str, typing.Any]) -> None:
        self.update_settings_calls.append(settings)

    def set_configuration_resolver(self, resolver: typing.Any) -> None:
        self.resolver = resolver

    async def ensure_started(self, _root_uri: str) -> None:
        self.ensure_started_calls += 1

    async def send_settings(self) -> None:
        self.send_settings_calls += 1

    async def add_workspace_folder(self, folder_uri: str, name: str) -> None:
        self.added_folders.append((folder_uri, name))

    async def sync_watched_files(
        self, file_paths: typing.Sequence[Path], recheck_timeout: float
    ) -> set[Path]:
        self.sweeps.append((list(file_paths), recheck_timeout))
        return set()

    async def dispose(self) -> None:
        self.disposed += 1


class _FakeWorkspaceActionRunner:
    def __init__(self) -> None:
        self.calls: list[
            tuple[typing.Any, typing.Any, list[Path] | None, code_action.RunActionMeta]
        ] = []
        self.responses: dict[typing.Any, typing.Any] = {
            AnnouncePyreflyProjectAction: AnnouncePyreflyProjectResult(
                registered=True
            )
        }

    async def run_action_in_projects(
        self,
        action_type: typing.Any,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        project_paths: list[Path] | None = None,
        concurrently: bool = True,
    ) -> dict[Path, typing.Any]:
        _ = concurrently  # part of the protocol; these tests never fan out
        self.calls.append((action_type, payload, project_paths, meta))
        return dict.fromkeys(project_paths or [], self.responses[action_type])


def _identity(
    project_dir: Path,
    *,
    version: str = "1.3.0",
    topology: PyreflyTopology = PyreflyTopology.WORKSPACE,
) -> PyreflyProjectIdentity:
    return PyreflyProjectIdentity.build(
        project_path=project_dir.as_posix(),
        version=version,
        topology=topology,
        settings={
            "pythonPath": "/venv/bin/python",
            "extraPaths": ["/venv/lib/site-packages"],
            "configPath": "/cache/pyrefly.toml",
        },
    )


async def _service(
    project_dir: Path,
    *,
    root: Path | None,
    declares: bool = True,
    topology: str = "workspace",
    version: str = "1.3.0",
    inner: _RecordingInnerLspService | None = None,
    runner: _FakeWorkspaceActionRunner | None = None,
) -> tuple[PyreflyLspService, _RecordingInnerLspService, _FakeWorkspaceActionRunner]:
    recorder = inner or _RecordingInnerLspService()
    action_runner = runner or _FakeWorkspaceActionRunner()
    with (
        mock.patch(
            "fine_python_pyrefly.pyrefly_lsp_service.LspService",
            return_value=recorder,
        ),
        mock.patch.object(importlib.metadata, "version", return_value=version),
    ):
        service = PyreflyLspService(
            lsp_client=typing.cast(typing.Any, object()),
            file_editor=typing.cast(typing.Any, object()),
            logger=NoOpLogger(),
            extension_runner_info_provider=typing.cast(
                typing.Any, _FakeExtensionRunnerInfoProvider()
            ),
            work_slots=typing.cast(typing.Any, _NoopWorkSlots()),
            pyrefly_config=typing.cast(
                typing.Any,
                _FakePyreflyConfig(Path("/cache/pyrefly.toml"), topology),
            ),
            project_info_provider=typing.cast(
                typing.Any, _FakeProjectInfoProvider(project_dir)
            ),
            workspace_info_provider=typing.cast(
                typing.Any, _FakeWorkspaceInfoProvider(root, declares=declares)
            ),
            workspace_action_runner=typing.cast(typing.Any, action_runner),
        )
        await service.init()
    return service, recorder, action_runner


async def test_facade_announces_on_init_and_forwards_a_query(tmp_path: Path) -> None:
    """A non-root project registers on init and every IDE query goes to the root.

    The announce is what makes the host know the project's interpreter and
    config; a query that answered locally would silently use a server the
    workspace agreed not to start.
    """
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    result = QueryPyreflyHostResult(raw={"contents": "int"})
    runner = _FakeWorkspaceActionRunner()
    runner.responses[QueryPyreflyHostAction] = result
    service, _, _ = await _service(project, root=root, runner=runner)

    assert [call[0] for call in runner.calls] == [AnnouncePyreflyProjectAction]
    announce_payload = runner.calls[0][1]
    assert isinstance(announce_payload, AnnouncePyreflyProjectPayload)
    assert announce_payload.identity.project_path == project.as_posix()
    assert runner.calls[0][2] == [root]

    raw = await service.get_hover(
        project / "a.py", "x = 1\n", {"line": 0, "character": 0}
    )

    assert raw == {"contents": "int"}
    query_call = runner.calls[-1]
    assert query_call[0] is QueryPyreflyHostAction
    assert query_call[2] == [root]
    query_payload = query_call[1]
    assert isinstance(query_payload, QueryPyreflyHostPayload)
    assert query_payload.query.kind == PyreflyQueryKind.HOVER


async def test_facade_check_goes_through_the_forward_not_check_file(
    tmp_path: Path,
) -> None:
    """Workspace checks are the handler's chunked forwards, not a local check.

    A local ``check_file`` in a forwarding project would start a second server
    and answer from it, which is the topology this mode exists to remove.
    """
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    outcome = FileCheckOutcome(
        uri=ResourceUri((project / "a.py").as_uri()),
        status=FileCheckStatus.CHECKED,
        diagnostics=[],
    )
    runner = _FakeWorkspaceActionRunner()
    runner.responses[CheckFilesOnPyreflyHostAction] = CheckFilesOnPyreflyHostResult(
        files=[outcome]
    )
    service, _, _ = await _service(project, root=root, runner=runner)

    with pytest.raises(code_action.ActionFailedException):
        await service.check_file(project / "a.py")

    outcomes = await service.forward_check_chunk(
        [outcome.uri], {outcome.uri: "x = 1\n"}, sweep=True
    )

    assert outcomes == [outcome]
    chunk_call = runner.calls[-1]
    assert chunk_call[0] is CheckFilesOnPyreflyHostAction
    payload = chunk_call[1]
    assert payload.sweep is True


async def test_init_refuses_a_workspace_without_a_root(tmp_path: Path) -> None:
    """No root must be a configuration error, never a fallback to N servers."""
    project = tmp_path / "pkg"
    project.mkdir()
    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _service(project, root=None)
    assert "workspace root" in exc_info.value.message
    assert "Never fall back" in exc_info.value.message


async def test_init_refuses_a_project_outside_the_root(tmp_path: Path) -> None:
    """A project outside the root cannot forward to it."""
    root = tmp_path / "ws"
    project = tmp_path / "other"
    root.mkdir()
    project.mkdir()
    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _service(project, root=root)
    assert "outside the workspace root" in exc_info.value.message


async def test_forward_refuses_a_root_without_the_host_action(
    tmp_path: Path,
) -> None:
    """A root that does not declare the host action cannot serve forwards."""
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _service(project, root=root, declares=False)
    assert "does not declare the pyrefly host action" in exc_info.value.message


def test_host_refuses_a_version_mismatch_without_registering(
    tmp_path: Path,
) -> None:
    """One pyrefly major.minor per workspace; the refusal names both sides."""
    root = tmp_path / "ws"
    root.mkdir()
    service = _host_service_sync(root, host_version="1.3.0")

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        service.register_project(_identity(root / "pkg", version="1.4.0"))

    assert "1.4.0" in exc_info.value.message
    assert "1.3.0" in exc_info.value.message
    assert "per-project" in exc_info.value.message


def test_host_refuses_a_mixed_topology(tmp_path: Path) -> None:
    """A per-project announcement is a mixed topology, not a silent ignore."""
    root = tmp_path / "ws"
    root.mkdir()
    service = _host_service_sync(root, host_version="1.3.0")

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        service.register_project(
            _identity(root / "pkg", topology=PyreflyTopology.PER_PROJECT)
        )

    assert "Mixed pyrefly topologies" in exc_info.value.message


def test_host_refuses_a_project_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    service = _host_service_sync(root, host_version="1.3.0")

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        service.register_project(_identity(tmp_path / "elsewhere"))

    assert "outside the workspace root" in exc_info.value.message


def test_host_accepts_matching_major_minor_and_patch_differences(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    service = _host_service_sync(root, host_version="1.3.2")

    service.register_project(_identity(root / "pkg", version="1.3.0"))

    assert service._project_registry != {}


def _host_service_sync(root: Path, *, host_version: str) -> PyreflyLspService:
    """A host service built without awaiting ``init`` for register-only tests."""
    with mock.patch.object(importlib.metadata, "version", return_value=host_version):
        service = PyreflyLspService(
            lsp_client=typing.cast(typing.Any, object()),
            file_editor=typing.cast(typing.Any, object()),
            logger=NoOpLogger(),
            extension_runner_info_provider=typing.cast(
                typing.Any, _FakeExtensionRunnerInfoProvider()
            ),
            work_slots=typing.cast(typing.Any, _NoopWorkSlots()),
            pyrefly_config=typing.cast(
                typing.Any,
                _FakePyreflyConfig(Path("/cache/pyrefly.toml"), "workspace"),
            ),
            project_info_provider=typing.cast(
                typing.Any, _FakeProjectInfoProvider(root)
            ),
            workspace_info_provider=typing.cast(
                typing.Any, _FakeWorkspaceInfoProvider(root)
            ),
            workspace_action_runner=typing.cast(
                typing.Any, _FakeWorkspaceActionRunner()
            ),
        )
    # `_is_host`/`_root_dir` come from `init`; set the same values it would.
    service._is_host = True
    service._root_dir = root
    return service


async def test_first_forward_adds_the_folder_and_waits_for_the_pull(
    tmp_path: Path,
) -> None:
    """The first forward adds the folder, pushes, and returns only after the
    server has pulled that folder's configuration.

    A forward that returned before the pull would answer against the previous
    configuration; one that never added the folder would have the server
    answer about the wrong project.
    """
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    service, recorder, _ = await _service(root, root=root)
    identity = _identity(project)

    task = asyncio.create_task(
        service.ensure_forwarded(identity, sweep_paths=[])
    )
    for _ in range(100):
        if recorder.added_folders:
            break
        await asyncio.sleep(0.01)
    assert recorder.added_folders == [(project.as_uri(), project.as_posix())]
    assert recorder.send_settings_calls == 1
    assert recorder.resolver is not None

    recorder.resolver(project.as_uri())
    await asyncio.wait_for(task, timeout=5)

    assert service._session_folder_fingerprints[project.as_uri()] == identity.fingerprint


async def test_an_unchanged_forward_completes_without_a_push(
    tmp_path: Path,
) -> None:
    """A forward whose folder is already current joins a flush with no work; it
    must complete immediately, not wait for a flush that will never have work."""
    root = tmp_path / "ws"
    root.mkdir()
    project = root / "pkg"
    project.mkdir()
    service, recorder, _ = await _service(root, root=root)
    identity = _identity(project)

    task = asyncio.create_task(service.ensure_forwarded(identity, sweep_paths=[]))
    for _ in range(100):
        if recorder.added_folders:
            break
        await asyncio.sleep(0.01)
    recorder.resolver(project.as_uri())
    await asyncio.wait_for(task, timeout=5)
    pushes = recorder.send_settings_calls

    await asyncio.wait_for(
        service.ensure_forwarded(identity, sweep_paths=[]), timeout=5
    )

    assert recorder.send_settings_calls == pushes
    assert len(recorder.added_folders) == 1


async def test_a_fingerprint_change_pushes_without_adding_the_folder_again(
    tmp_path: Path,
) -> None:
    """A changed identity is a push, not a restart or a duplicate folder."""
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    service, recorder, _ = await _service(root, root=root)
    first = _identity(project)

    task = asyncio.create_task(service.ensure_forwarded(first, sweep_paths=[]))
    for _ in range(100):
        if recorder.added_folders:
            break
        await asyncio.sleep(0.01)
    recorder.resolver(project.as_uri())
    await asyncio.wait_for(task, timeout=5)

    changed_identity = PyreflyProjectIdentity.build(
        project_path=first.project_path,
        version=first.version,
        topology=first.topology,
        settings={**first.settings, "displayTypeErrors": "force-on"},
    )
    task = asyncio.create_task(
        service.ensure_forwarded(changed_identity, sweep_paths=[])
    )
    for _ in range(100):
        if recorder.send_settings_calls >= 2:
            break
        await asyncio.sleep(0.01)
    recorder.resolver(project.as_uri())
    await asyncio.wait_for(task, timeout=5)

    assert len(recorder.added_folders) == 1
    assert recorder.send_settings_calls == 2
    assert (
        service._session_folder_fingerprints[project.as_uri()]
        == changed_identity.fingerprint
    )


async def test_a_missing_pull_answer_fails_the_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that never asks for the folder's configuration must fail the
    request, not let it answer against the previous settings."""
    monkeypatch.setattr(service_module, "_FOLDER_PULL_TIMEOUT_SEC", 0.05)
    root = tmp_path / "ws"
    project = root / "pkg"
    project.mkdir(parents=True)
    service, _, _ = await _service(root, root=root)

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await service.ensure_forwarded(_identity(project), sweep_paths=[])

    assert "workspace/configuration" in exc_info.value.message


async def test_idle_stop_disposes_and_rerenders_folders_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D11's idle window disposes the server, and the next demand re-adds the
    project's folder instead of assuming the dead session still holds it."""
    monkeypatch.setattr(service_module, "_IDLE_SCAN_SEC", 0.01)
    monkeypatch.setattr(service_module, "_IDLE_SHUTDOWN_SEC", 0.01)
    root = tmp_path / "ws"
    root.mkdir()
    service, recorder, _ = await _service(root, root=root)

    for _ in range(100):
        if recorder.disposed:
            break
        await asyncio.sleep(0.01)

    assert recorder.disposed >= 1
    assert service._session_folder_fingerprints == {}
    await service.dispose()
