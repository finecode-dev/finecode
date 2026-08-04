"""Tests for the cross-project aggregation the check_toolchains bridge performs.

`CheckToolchainsRunResult.update()` is a *within-project* merger (R-302): its axes are
keyed by env name, which is unique inside a project but not across them. This bridge is
the caller above the action layer, so keeping the projects apart is its job -- and the
env name shared by every matrix project (`testing`) is exactly the case that catches it.
"""

from __future__ import annotations

import pathlib

import pytest
from fine_envs.check_toolchains_action import CheckToolchainsRunResult
from fine_envs.sync_toolchains_action import EnvToolchainAxis
from fine_git_hooks import precommit_action
from fine_git_hooks.check_toolchains_precommit_bridge_handler import (
    CheckToolchainsPrecommitBridgeHandler,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)


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


class _FakeLogger:
    def info(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def _stale(declared: list[str], derived: list[str]) -> CheckToolchainsRunResult:
    return CheckToolchainsRunResult(
        stale_axes=[
            EnvToolchainAxis(env_name="testing", declared=declared, derived=derived)
        ]
    )


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


async def _run(
    project_results: dict[pathlib.Path, CheckToolchainsRunResult],
    staged_files: list[pathlib.Path] | None = None,
) -> precommit_action.PrecommitRunResult:
    project_paths = list(project_results)
    handler = CheckToolchainsPrecommitBridgeHandler(
        workspace_action_runner=_FakeWorkspaceActionRunner(project_results),
        workspace_info_provider=_FakeWorkspaceInfoProvider(project_paths),
        logger=_FakeLogger(),
    )
    if staged_files is None:
        staged_files = [path / "src" / "mod.py" for path in project_paths]
    return await handler.run(
        precommit_action.PrecommitRunPayload(), _run_context(staged_files)
    )


async def test_same_env_name_in_two_projects_is_reported_once_per_project(
    tmp_path: pathlib.Path,
) -> None:
    # regression: merging the per-project results with update() collapsed both into a
    # single `testing` axis carrying only the first project's versions
    project_a = tmp_path / "extensions" / "one"
    project_b = tmp_path / "presets" / "two"
    result = await _run(
        {
            project_a: _stale(["cpython@3.11"], ["cpython@3.11", "cpython@3.12"]),
            project_b: _stale(["cpython@3.9"], ["cpython@3.13"]),
        }
    )

    assert len(result.action_results) == 2
    reported = {
        key: entry.stale_axes[0] for key, entry in result.action_results.items()
    }
    # each project keeps its own axis values, not the first project's
    derived_by_label = {key: axis.derived for key, axis in reported.items()}
    assert sorted(derived_by_label.values()) == [
        ["cpython@3.11", "cpython@3.12"],
        ["cpython@3.13"],
    ]
    assert all("testing" == axis.env_name for axis in reported.values())


async def test_project_labels_stay_distinct_when_basenames_collide(
    tmp_path: pathlib.Path,
) -> None:
    # the labels are what keeps the entries apart in the action_results dict, so two
    # projects with the same directory name must not collapse into one key
    result = await _run(
        {
            tmp_path / "a" / "pkg": _stale(["cpython@3.11"], ["cpython@3.12"]),
            tmp_path / "b" / "pkg": _stale(["cpython@3.11"], ["cpython@3.13"]),
        }
    )

    assert len(result.action_results) == 2


async def test_drift_in_any_project_fails_the_commit(tmp_path: pathlib.Path) -> None:
    project_a = tmp_path / "clean"
    project_b = tmp_path / "drifted"
    result = await _run(
        {
            project_a: CheckToolchainsRunResult(),
            project_b: _stale(["cpython@3.11"], ["cpython@3.12"]),
        }
    )

    assert result.return_code == code_action.RunReturnCode.ERROR
    # only the drifted project is worth a report entry
    assert len(result.action_results) == 1
    assert "drifted" in next(iter(result.action_results))


async def test_no_drift_reports_a_single_up_to_date_entry(
    tmp_path: pathlib.Path,
) -> None:
    # the clean case must not get noisier just because several projects were checked
    result = await _run(
        {
            tmp_path / "one": CheckToolchainsRunResult(),
            tmp_path / "two": CheckToolchainsRunResult(),
        }
    )

    assert list(result.action_results) == ["check_toolchains"]
    assert result.return_code == code_action.RunReturnCode.SUCCESS


async def test_no_staged_files_is_a_no_op(tmp_path: pathlib.Path) -> None:
    result = await _run({tmp_path / "one": CheckToolchainsRunResult()}, staged_files=[])

    assert result.action_results == {}
    assert result.return_code == code_action.RunReturnCode.SUCCESS


async def test_staged_files_outside_every_project_is_a_no_op(
    tmp_path: pathlib.Path,
) -> None:
    result = await _run(
        {tmp_path / "one": _stale(["cpython@3.11"], ["cpython@3.12"])},
        staged_files=[tmp_path / "elsewhere" / "README.md"],
    )

    assert result.action_results == {}


async def test_missing_discovery_handler_is_an_error(tmp_path: pathlib.Path) -> None:
    handler = CheckToolchainsPrecommitBridgeHandler(
        workspace_action_runner=_FakeWorkspaceActionRunner({}),
        workspace_info_provider=_FakeWorkspaceInfoProvider([tmp_path]),
        logger=_FakeLogger(),
    )
    run_context = _run_context([])
    run_context.staged_files = None  # discovery never ran

    with pytest.raises(code_action.ActionFailedException, match="discovery handler"):
        await handler.run(precommit_action.PrecommitRunPayload(), run_context)
