"""The lint precommit bridge sends one workspace call with a complete payload
per project, instead of one call per project.

A 3-project staged set used to open three concurrent fan-outs, each carrying
its own file list. One call carries every project's files at once, and each
project's payload must contain exactly that project's files — a merge that
mixed them would lint one project's files in another project's environment.
"""

from __future__ import annotations

import pathlib

import pytest
from fine_lint.lint_action import LintRunPayload, LintRunResult, LintTarget
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_git_hooks import precommit_action
from fine_git_hooks.lint_precommit_bridge_handler import (
    LintPrecommitBridgeHandler,
)


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


class _RecordingWorkspaceActionRunner:
    def __init__(self, results: dict[pathlib.Path, LintRunResult]) -> None:
        self._results = results
        self.calls: list[tuple[type, dict[pathlib.Path, LintRunPayload]]] = []

    async def run_action_per_project(
        self,
        action_type: type,
        payload_by_project: dict[pathlib.Path, LintRunPayload],
        meta: code_action.RunActionMeta,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, LintRunResult]:
        self.calls.append((action_type, payload_by_project))
        return {
            path: self._results[path]
            for path in payload_by_project
            if path in self._results
        }


class _RaisingWorkspaceActionRunner:
    async def run_action_per_project(
        self, action_type, payload_by_project, meta, concurrently=True
    ):
        raise iprojectactionrunner.ActionRunFailed("boom")


class _FakeLogger:
    def info(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def _run_context(
    staged_files: list[pathlib.Path],
) -> precommit_action.PrecommitRunContext:
    run_context = precommit_action.PrecommitRunContext(
        run_id=1,
        initial_payload=precommit_action.PrecommitRunPayload(),
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=None,  # type: ignore[arg-type]
    )
    run_context.staged_files = staged_files
    return run_context


def _staged_files(projects: list[pathlib.Path]) -> list[pathlib.Path]:
    return [project / "src" / f"{project.name}.py" for project in projects]


async def test_lint_makes_one_call_with_per_project_payloads(
    tmp_path: pathlib.Path,
) -> None:
    project_a = tmp_path / "one"
    project_b = tmp_path / "two"
    project_c = tmp_path / "three"
    projects = [project_a, project_b, project_c]
    for project in projects:
        (project / "src").mkdir(parents=True)
    staged = _staged_files(projects)
    for path in staged:
        path.touch()

    runner = _RecordingWorkspaceActionRunner(
        {project: LintRunResult(messages={}) for project in projects}
    )
    handler = LintPrecommitBridgeHandler(
        workspace_action_runner=runner,
        workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
        logger=_FakeLogger(),
    )

    await handler.run(precommit_action.PrecommitRunPayload(), _run_context(staged))

    assert len(runner.calls) == 1
    _, payload_by_project = runner.calls[0]
    assert set(payload_by_project) == set(projects)
    for project, payload in payload_by_project.items():
        assert payload.target == LintTarget.FILES
        assert payload.file_paths == [
            path_to_resource_uri(project / "src" / f"{project.name}.py")
        ]


async def test_lint_failure_keeps_existing_prefix_byte_for_byte(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "one"
    (project / "src").mkdir(parents=True)
    staged = _staged_files([project])
    for path in staged:
        path.touch()

    handler = LintPrecommitBridgeHandler(
        workspace_action_runner=_RaisingWorkspaceActionRunner(),
        workspace_info_provider=_FakeWorkspaceInfoProvider([project]),
        logger=_FakeLogger(),
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(precommit_action.PrecommitRunPayload(), _run_context(staged))

    assert str(exc_info.value) == "Lint failed:\n  - boom"
