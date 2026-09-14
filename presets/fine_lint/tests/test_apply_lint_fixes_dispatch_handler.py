"""Tests for the workspace-level ``apply_lint_fixes`` dispatch handler."""

from __future__ import annotations

import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_lint.apply_lint_fixes_action import ApplyLintFixesRunPayload
from fine_lint.apply_lint_fixes_dispatch_handler import ApplyLintFixesDispatchHandler
from fine_lint.apply_lint_fixes_files_action import (
    ApplyLintFixesFilesAction,
    ApplyLintFixesFilesRunResult,
    ConvergenceStatus,
)
from fine_lint.lint_action import LintTarget


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


class _FakeWorkspaceActionRunner:
    """Answers ``run_action_in_projects`` for whichever action type is asked, and
    records every call so a test can assert nothing fanned out."""

    def __init__(
        self,
        results_by_action: dict[type, dict[pathlib.Path, code_action.RunActionResult]]
        | None = None,
    ) -> None:
        self._results_by_action = results_by_action or {}
        self.calls: list[tuple[type, list[pathlib.Path]]] = []

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, code_action.RunActionResult]:
        assert project_paths is not None
        self.calls.append((action_type, list(project_paths)))
        results = self._results_by_action.get(action_type, {})
        return {path: results[path] for path in project_paths if path in results}

    async def run_action_per_project(
        self,
        action_type: type,
        payload_by_project: dict[pathlib.Path, code_action.RunActionPayload],
        meta: code_action.RunActionMeta,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, code_action.RunActionResult]:
        self.calls.append((action_type, list(payload_by_project)))
        results = self._results_by_action.get(action_type, {})
        return {path: results[path] for path in payload_by_project if path in results}


class _FakeUserMessenger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...


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


def _run_context(
    sender: _CollectingPartialResultSender,
    trigger: code_action.RunActionTrigger = code_action.RunActionTrigger.USER,
) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(trigger=trigger, dev_env=code_action.DevEnv.CI)
        partial_result_sender = sender

    return _RunContext()


async def _run(
    payload: ApplyLintFixesRunPayload,
    known_project_paths: list[pathlib.Path],
    results_by_action: dict[type, dict[pathlib.Path, code_action.RunActionResult]]
    | None = None,
    trigger: code_action.RunActionTrigger = code_action.RunActionTrigger.USER,
) -> tuple[
    _CollectingPartialResultSender, _FakeWorkspaceActionRunner, _FakeUserMessenger
]:
    action_runner = _FakeWorkspaceActionRunner(results_by_action)
    user_messenger = _FakeUserMessenger()
    handler = ApplyLintFixesDispatchHandler(
        workspace_action_runner=action_runner,  # type: ignore[arg-type]
        workspace_info_provider=_FakeWorkspaceInfoProvider(known_project_paths),
        logger=_FakeLogger(),  # type: ignore[arg-type]
        user_messenger=user_messenger,  # type: ignore[arg-type]
    )
    sender = _CollectingPartialResultSender()
    await handler.run(payload, _run_context(sender, trigger))  # type: ignore[arg-type]
    return sender, action_runner, user_messenger


async def test_target_files_with_empty_file_paths_never_fans_out(
    tmp_path: pathlib.Path,
) -> None:
    """Asking to fix specific files with an empty file list must do nothing at
    all -- not run any project's pass loop and not report anything -- rather
    than silently treating an empty request as "fix everything"."""
    project = tmp_path / "pkg"
    sender, action_runner, _ = await _run(
        ApplyLintFixesRunPayload(target=LintTarget.FILES, file_paths=[]),
        known_project_paths=[project],
    )

    assert action_runner.calls == []
    assert sender.results == []


async def test_files_outside_every_project_warn_a_user_run(
    tmp_path: pathlib.Path,
) -> None:
    """Asking to fix specific files that belong to no known project must tell
    the user why nothing happened -- otherwise a stale project list or a typoed
    path looks identical to "already clean"."""
    project = tmp_path / "pkg"
    sender, action_runner, user_messenger = await _run(
        ApplyLintFixesRunPayload(
            target=LintTarget.FILES,
            file_paths=[path_to_resource_uri(tmp_path / "elsewhere" / "mod.py")],
        ),
        known_project_paths=[project],
    )

    assert action_runner.calls == []
    assert sender.results == []
    assert len(user_messenger.warnings) == 1
    assert "elsewhere" in user_messenger.warnings[0]


async def test_files_outside_every_project_stay_quiet_for_system_runs(
    tmp_path: pathlib.Path,
) -> None:
    """The same unmatched-files case must not bother the user when the request
    came from the system (e.g. an editor firing for every open buffer) rather
    than a person -- that is expected background noise, not a diagnosable
    problem."""
    project = tmp_path / "pkg"
    _, action_runner, user_messenger = await _run(
        ApplyLintFixesRunPayload(
            target=LintTarget.FILES,
            file_paths=[path_to_resource_uri(tmp_path / "elsewhere" / "mod.py")],
        ),
        known_project_paths=[project],
        trigger=code_action.RunActionTrigger.SYSTEM,
    )

    assert action_runner.calls == []
    assert user_messenger.warnings == []


async def test_files_matching_a_project_are_forwarded_to_its_pass_loop(
    tmp_path: pathlib.Path,
) -> None:
    """A file that does belong to a known project must reach that project's
    fix loop -- the dispatch handler's whole job is to get it there."""
    project = tmp_path / "pkg"
    file_uri = path_to_resource_uri(project / "mod.py")
    canned_result = ApplyLintFixesFilesRunResult(
        applied_counts={file_uri: 1},
        status=ConvergenceStatus.CONVERGED,
        passes=1,
    )
    sender, action_runner, user_messenger = await _run(
        ApplyLintFixesRunPayload(target=LintTarget.FILES, file_paths=[file_uri]),
        known_project_paths=[project],
        results_by_action={ApplyLintFixesFilesAction: {project: canned_result}},
    )

    assert action_runner.calls == [(ApplyLintFixesFilesAction, [project])]
    assert user_messenger.warnings == []
    assert len(sender.results) == 1
    assert sender.results[0].applied_counts == {file_uri: 1}
