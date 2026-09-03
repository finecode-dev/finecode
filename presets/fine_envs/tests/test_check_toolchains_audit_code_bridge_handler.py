"""Tests for the flattening the check_toolchains -> audit_code bridge performs.

`CheckToolchainsRunResult` is structured (declared/derived per env) while audit_code
speaks diagnostics keyed by file, so the bridge is where the two contracts meet (R-305).
Two properties matter: drift must survive the conversion as an ERROR diagnostic (that is
what fails CI), and each project's findings must stay anchored at its own definition file
rather than merging into one entry (R-302).
"""

from __future__ import annotations

import pathlib

from fine_audit_code.audit_code_action import (
    AuditCodeRunPayload,
    AuditCodeRunResult,
    AuditCodeTarget,
)
from fine_inspect_code.diagnostic_types import DiagnosticSeverity
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

from fine_envs.check_toolchains_action import CheckToolchainsRunResult
from fine_envs.check_toolchains_audit_code_bridge_handler import (
    CheckToolchainsAuditCodeBridgeHandler,
)
from fine_envs.sync_toolchains_action import EnvToolchainAxis


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


class _FakeWorkspaceActionRunner:
    """Answers with a canned per-project result, as the real fan-out would."""

    def __init__(self, results: dict[pathlib.Path, CheckToolchainsRunResult]) -> None:
        self._results = results
        self.requested_project_paths: list[pathlib.Path] = []

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, CheckToolchainsRunResult]:
        assert project_paths is not None
        self.requested_project_paths.extend(project_paths)
        return {
            path: self._results[path] for path in project_paths if path in self._results
        }

    async def run_action_per_project(
        self,
        action_type: type,
        payload_by_project: dict[pathlib.Path, code_action.RunActionPayload],
        meta: code_action.RunActionMeta,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, CheckToolchainsRunResult]:
        self.requested_project_paths.extend(payload_by_project)
        return {
            path: self._results[path]
            for path in payload_by_project
            if path in self._results
        }


class _FakeUserMessenger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...


class _CollectingPartialResultSender:
    def __init__(self) -> None:
        self.results: list[AuditCodeRunResult] = []

    async def send(self, result: AuditCodeRunResult) -> None:
        self.results.append(result)


def _def_uri(project_path: pathlib.Path) -> ResourceUri:
    return path_to_resource_uri(project_path / "pyproject.toml")


def _stale(
    project_path: pathlib.Path, declared: list[str], derived: list[str]
) -> CheckToolchainsRunResult:
    return CheckToolchainsRunResult(
        stale_axes=[
            EnvToolchainAxis(env_name="testing", declared=declared, derived=derived)
        ],
        project_def_path=_def_uri(project_path),
    )


def _clean(project_path: pathlib.Path) -> CheckToolchainsRunResult:
    return CheckToolchainsRunResult(project_def_path=_def_uri(project_path))


def _run_context(
    sender: _CollectingPartialResultSender,
    trigger: code_action.RunActionTrigger = code_action.RunActionTrigger.USER,
) -> object:
    class _RunContext:
        meta = code_action.RunActionMeta(trigger=trigger, dev_env=code_action.DevEnv.CI)
        partial_result_sender = sender

    return _RunContext()


async def _run(
    project_results: dict[pathlib.Path, CheckToolchainsRunResult],
    payload: AuditCodeRunPayload | None = None,
    known_project_paths: list[pathlib.Path] | None = None,
    trigger: code_action.RunActionTrigger = code_action.RunActionTrigger.USER,
) -> tuple[
    _CollectingPartialResultSender,
    _FakeWorkspaceActionRunner,
    _FakeUserMessenger,
    _FakeLogger,
]:
    action_runner = _FakeWorkspaceActionRunner(project_results)
    user_messenger = _FakeUserMessenger()
    logger = _FakeLogger()
    handler = CheckToolchainsAuditCodeBridgeHandler(
        workspace_action_runner=action_runner,
        workspace_info_provider=_FakeWorkspaceInfoProvider(
            known_project_paths
            if known_project_paths is not None
            else list(project_results)
        ),
        user_messenger=user_messenger,
        logger=logger,
    )
    sender = _CollectingPartialResultSender()
    await handler.run(
        payload if payload is not None else AuditCodeRunPayload(),
        _run_context(sender, trigger),  # type: ignore[arg-type]
    )
    return sender, action_runner, user_messenger, logger


def _merged(sender: _CollectingPartialResultSender) -> AuditCodeRunResult:
    merged = AuditCodeRunResult(messages={})
    for result in sender.results:
        merged.update(result)
    return merged


async def test_drift_becomes_an_error_diagnostic_at_the_definition_file(
    tmp_path: pathlib.Path,
) -> None:
    # the whole point of the bridge: drift has to survive as something audit_code fails on
    project = tmp_path / "pkg"
    sender, _, _, _ = await _run(
        {project: _stale(project, ["cpython@3.11"], ["cpython@3.11", "cpython@3.12"])}
    )

    result = _merged(sender)
    diagnostics = result.messages[_def_uri(project)]
    assert len(diagnostics) == 1
    assert diagnostics[0].severity == DiagnosticSeverity.ERROR
    assert "testing" in diagnostics[0].message
    assert "cpython@3.12" in diagnostics[0].message
    assert result.return_code == code_action.RunReturnCode.ERROR


async def test_two_projects_keep_their_own_axes(tmp_path: pathlib.Path) -> None:
    # regression guard mirroring the precommit bridge: `testing` is the env name every
    # matrix project shares, so a merge keyed by env name would collapse the two
    project_a = tmp_path / "extensions" / "one"
    project_b = tmp_path / "presets" / "two"
    sender, _, _, _ = await _run(
        {
            project_a: _stale(project_a, ["cpython@3.11"], ["cpython@3.12"]),
            project_b: _stale(project_b, ["cpython@3.9"], ["cpython@3.13"]),
        }
    )

    messages = _merged(sender).messages
    assert set(messages) == {_def_uri(project_a), _def_uri(project_b)}
    assert "cpython@3.12" in messages[_def_uri(project_a)][0].message
    assert "cpython@3.13" in messages[_def_uri(project_b)][0].message


async def test_clean_project_is_reported_as_checked_and_empty(
    tmp_path: pathlib.Path,
) -> None:
    # an absent key is indistinguishable from "never checked" and leaves stale
    # diagnostics in an editor; an empty list clears them and renders "<path>: OK"
    project = tmp_path / "pkg"
    sender, _, _, _ = await _run({project: _clean(project)})

    result = _merged(sender)
    assert result.messages == {_def_uri(project): []}
    assert result.return_code == code_action.RunReturnCode.SUCCESS


async def test_file_target_checks_only_the_owning_projects(
    tmp_path: pathlib.Path,
) -> None:
    checked = tmp_path / "checked"
    untouched = tmp_path / "untouched"
    _, action_runner, _, _ = await _run(
        {
            checked: _stale(checked, ["cpython@3.11"], ["cpython@3.12"]),
            untouched: _stale(untouched, ["cpython@3.11"], ["cpython@3.12"]),
        },
        payload=AuditCodeRunPayload(
            target=AuditCodeTarget.FILES,
            file_paths=[path_to_resource_uri(checked / "src" / "mod.py")],
        ),
    )

    assert action_runner.requested_project_paths == [checked]


async def test_file_target_without_files_never_fans_out(
    tmp_path: pathlib.Path,
) -> None:
    # R-309: the empty-input decision belongs here, not in each project's ER
    project = tmp_path / "pkg"
    sender, action_runner, _, _ = await _run(
        {project: _stale(project, ["cpython@3.11"], ["cpython@3.12"])},
        payload=AuditCodeRunPayload(target=AuditCodeTarget.FILES, file_paths=[]),
    )

    assert action_runner.requested_project_paths == []
    assert sender.results == []


async def test_files_outside_every_project_warn_a_user_run(
    tmp_path: pathlib.Path,
) -> None:
    project = tmp_path / "pkg"
    _, action_runner, user_messenger, _ = await _run(
        {project: _clean(project)},
        payload=AuditCodeRunPayload(
            target=AuditCodeTarget.FILES,
            file_paths=[path_to_resource_uri(tmp_path / "elsewhere" / "README.md")],
        ),
    )

    assert action_runner.requested_project_paths == []
    assert len(user_messenger.warnings) == 1
    assert "elsewhere" in user_messenger.warnings[0]


async def test_files_outside_every_project_stay_quiet_for_system_runs(
    tmp_path: pathlib.Path,
) -> None:
    # R-505: editors fire audit_code for buffers outside any project; that is expected
    project = tmp_path / "pkg"
    _, _, user_messenger, _ = await _run(
        {project: _clean(project)},
        payload=AuditCodeRunPayload(
            target=AuditCodeTarget.FILES,
            file_paths=[path_to_resource_uri(tmp_path / "elsewhere" / "README.md")],
        ),
        trigger=code_action.RunActionTrigger.SYSTEM,
    )

    assert user_messenger.warnings == []


async def test_result_without_definition_path_still_reports_the_drift(
    tmp_path: pathlib.Path,
) -> None:
    # a third-party check_toolchains handler may not fill project_def_path; a coarse
    # anchor is acceptable, silently dropping a failure is not
    project = tmp_path / "pkg"
    sender, _, _, logger = await _run(
        {
            project: CheckToolchainsRunResult(
                stale_axes=[
                    EnvToolchainAxis(
                        env_name="testing",
                        declared=["cpython@3.11"],
                        derived=["cpython@3.12"],
                    )
                ]
            )
        }
    )

    result = _merged(sender)
    assert result.messages[path_to_resource_uri(project)][0].severity == (
        DiagnosticSeverity.ERROR
    )
    assert len(logger.warnings) == 1
