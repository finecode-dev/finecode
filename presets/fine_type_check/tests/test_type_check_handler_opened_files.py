"""The IDE opened-files branch covers only the run's own projects.

A narrowed run (one project of an ``inspect_code`` bridge) must not report the
other projects' open files. An opened file with no entry in a project's partial
result is sent as an empty list, and the IDE clears that file's diagnostics —
so a foreign file in a narrowed run would blank a buffer another project owns.
"""

from __future__ import annotations

import pathlib
import typing

from fine_inspect_code.diagnostic_types import DiagnosticFilesRunResult
from fine_src_artifacts.list_src_artifact_files_by_lang_action import (
    ListSrcArtifactFilesByLangAction,
)
from fine_type_check.type_check_action import TypeCheckRunPayload, TypeCheckTarget
from fine_type_check.type_check_files_action import TypeCheckFilesAction
from fine_type_check.type_check_handler import TypeCheckHandler, TypeCheckHandlerConfig
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from finecode_extension_api import code_action


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


class _FakeFileEditor:
    def __init__(self, opened_files: list[pathlib.Path]) -> None:
        self._opened_files = opened_files

    def get_opened_files(self) -> list[pathlib.Path]:
        return list(self._opened_files)


class _FakeLogger:
    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class _RecordedCall:
    def __init__(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        project_paths: list[pathlib.Path],
    ) -> None:
        self.action_type = action_type
        self.payload = payload
        self.project_paths = project_paths


class _RecordingWorkspaceActionRunner:
    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, code_action.RunActionResult]:
        assert project_paths is not None
        self.calls.append(_RecordedCall(action_type, payload, list(project_paths)))
        if action_type is TypeCheckFilesAction:
            return {
                path: DiagnosticFilesRunResult(
                    messages={uri: [] for uri in payload.file_paths}
                )
                for path in project_paths
            }
        return {}


class _CollectingPartialResultSender:
    def __init__(self) -> None:
        self.results: list[code_action.RunActionResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.results.append(result)


class _FakeProgress:
    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def advance(self, steps: int, message: str | None = None) -> None: ...


class _FakeRunContext:
    def __init__(
        self,
        sender: _CollectingPartialResultSender,
        trigger: code_action.RunActionTrigger,
        dev_env: code_action.DevEnv,
    ) -> None:
        self.meta = code_action.RunActionMeta(trigger=trigger, dev_env=dev_env)
        self.partial_result_sender = sender

    def progress(
        self, title: str, *, total: int | None = None, cancellable: bool = False
    ) -> _FakeProgress:
        return _FakeProgress()


def _handler(
    action_runner: _RecordingWorkspaceActionRunner,
    project_paths: list[pathlib.Path],
    opened_files: list[pathlib.Path],
) -> TypeCheckHandler:
    return TypeCheckHandler(
        config=TypeCheckHandlerConfig(),
        workspace_action_runner=action_runner,  # type: ignore[arg-type]
        workspace_info_provider=_FakeWorkspaceInfoProvider(project_paths),
        file_editor=_FakeFileEditor(opened_files),  # type: ignore[arg-type]
        logger=_FakeLogger(),  # type: ignore[arg-type]
    )


async def test_narrowed_ide_run_checks_only_its_own_opened_files(
    tmp_path: pathlib.Path,
) -> None:
    """With the run narrowed to project A, only A's open files are checked and
    the other project's open file is never sent as an empty result."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    file_a = project_a / "mod.py"
    file_b = project_b / "mod.py"
    action_runner = _RecordingWorkspaceActionRunner()
    sender = _CollectingPartialResultSender()

    await _handler(
        action_runner, [project_a, project_b], [file_a, file_b]
    ).run(
        TypeCheckRunPayload(
            target=TypeCheckTarget.PROJECT,
            project_paths=[path_to_resource_uri(project_a)],
        ),
        _FakeRunContext(
            sender, code_action.RunActionTrigger.SYSTEM, code_action.DevEnv.IDE
        ),  # type: ignore[arg-type]
    )

    assert len(action_runner.calls) == 1
    call = action_runner.calls[0]
    assert call.action_type == TypeCheckFilesAction
    assert call.project_paths == [project_a]
    assert call.payload.file_paths == [path_to_resource_uri(file_a)]
    assert ListSrcArtifactFilesByLangAction not in [
        recorded.action_type for recorded in action_runner.calls
    ]
    sent_uris = {uri for result in sender.results for uri in result.messages}
    assert path_to_resource_uri(file_b) not in sent_uris


async def test_unscoped_ide_run_checks_opened_files_in_every_project(
    tmp_path: pathlib.Path,
) -> None:
    """Without a project narrowing the IDE path still checks every project's
    open files — the filter must not shrink the common direct-check case."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    file_a = project_a / "mod.py"
    file_b = project_b / "mod.py"
    action_runner = _RecordingWorkspaceActionRunner()
    sender = _CollectingPartialResultSender()

    await _handler(
        action_runner, [project_a, project_b], [file_a, file_b]
    ).run(
        TypeCheckRunPayload(target=TypeCheckTarget.PROJECT, project_paths=None),
        _FakeRunContext(
            sender, code_action.RunActionTrigger.SYSTEM, code_action.DevEnv.IDE
        ),  # type: ignore[arg-type]
    )

    check_calls = [
        call for call in action_runner.calls if call.action_type == TypeCheckFilesAction
    ]
    checked_uris = {uri for call in check_calls for uri in call.payload.file_paths}
    assert checked_uris == {
        path_to_resource_uri(file_a),
        path_to_resource_uri(file_b),
    }
