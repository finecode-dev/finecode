"""The type_check bridge narrows each per-project dispatch to that one project.

``type_check`` is workspace-scoped, but the bridge dispatches it once per
project. Without narrowing the payload the nested instance re-resolves the
whole workspace and gathers across it, so every project is type-checked once
per project: N instances x N projects. Narrowing keeps each nested gather to
the project that asked for it.
"""

from __future__ import annotations

import pathlib

from fine_inspect_code.inspect_code_action import (
    InspectCodeRunPayload,
    InspectCodeTarget,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_type_check.type_check_action import (
    TypeCheckAction,
    TypeCheckRunPayload,
    TypeCheckRunResult,
)
from fine_type_check.type_check_inspect_code_bridge_handler import (
    TypeCheckInspectCodeBridgeHandler,
)


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


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


class _FakeWorkspaceActionRunner:
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
        return {path: TypeCheckRunResult(messages={}) for path in project_paths}


class _FakeLogger:
    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class _CollectingPartialResultSender:
    def __init__(self) -> None:
        self.results: list[code_action.RunActionResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.results.append(result)


def _run_context(sender: _CollectingPartialResultSender) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        partial_result_sender = sender

    return _RunContext()


async def _run(
    payload: InspectCodeRunPayload,
    known_project_paths: list[pathlib.Path],
) -> tuple[_FakeWorkspaceActionRunner, _CollectingPartialResultSender]:
    action_runner = _FakeWorkspaceActionRunner()
    handler = TypeCheckInspectCodeBridgeHandler(
        workspace_action_runner=action_runner,  # type: ignore[arg-type]
        workspace_info_provider=_FakeWorkspaceInfoProvider(known_project_paths),
        logger=_FakeLogger(),  # type: ignore[arg-type]
    )
    sender = _CollectingPartialResultSender()
    await handler.run(payload, _run_context(sender))  # type: ignore[arg-type]
    return action_runner, sender


async def test_project_target_narrows_each_dispatch_to_its_project(
    tmp_path: pathlib.Path,
) -> None:
    """One TypeCheckAction per known project, each carrying only its own
    project path — the narrowing that keeps a nested workspace-wide
    type_check from multiplying into N²."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"

    action_runner, _ = await _run(
        InspectCodeRunPayload(target=InspectCodeTarget.PROJECT, project_paths=None),
        [project_a, project_b],
    )

    assert [call.action_type for call in action_runner.calls] == [
        TypeCheckAction,
        TypeCheckAction,
    ]
    by_project = {call.project_paths[0]: call for call in action_runner.calls}
    assert set(by_project) == {project_a, project_b}
    for project_path, call in by_project.items():
        assert isinstance(call.payload, TypeCheckRunPayload)
        assert call.payload.project_paths == [path_to_resource_uri(project_path)]


async def test_files_target_narrows_payload_to_the_owning_project(
    tmp_path: pathlib.Path,
) -> None:
    """A file-scoped call still narrows the dispatched payload to the owning
    project, so the nested type_check does not resolve every project for one
    file."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    file_a = project_a / "mod.py"

    action_runner, _ = await _run(
        InspectCodeRunPayload(
            target=InspectCodeTarget.FILES,
            file_paths=[path_to_resource_uri(file_a)],
            project_paths=None,
        ),
        [project_a, project_b],
    )

    assert len(action_runner.calls) == 1
    call = action_runner.calls[0]
    assert call.action_type == TypeCheckAction
    assert call.project_paths == [project_a]
    assert isinstance(call.payload, TypeCheckRunPayload)
    assert call.payload.project_paths == [path_to_resource_uri(project_a)]
    assert call.payload.file_paths == [path_to_resource_uri(file_a)]
