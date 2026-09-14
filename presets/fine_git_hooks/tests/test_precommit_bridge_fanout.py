"""Every precommit bridge makes one workspace call for the whole staged set.

The file-based bridges (`lint`, `format`, `type_check`, `inspect_code`,
`audit_code`) each send one ``run_action_per_project`` call carrying a complete
payload per project. The project-level bridge (`check_toolchains`) sends one
``run_action_in_projects`` call with the project list and no per-project
payloads. A 3-project staged set must produce exactly one call, with each
project's payload carrying only its own files — a merge that mixed them would
run one project's files in another project's environment.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pytest
from fine_audit_code.audit_code_action import AuditCodeRunResult, AuditCodeTarget
from fine_envs.check_toolchains_action import CheckToolchainsRunResult
from fine_format import FormatTarget, check_formatting_action
from fine_inspect_code.inspect_code_action import (
    InspectCodeRunResult,
    InspectCodeTarget,
)
from fine_type_check.type_check_action import TypeCheckRunResult, TypeCheckTarget
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    ProjectConfigStatus,
    WorkspaceProject,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_git_hooks import precommit_action
from fine_git_hooks.audit_code_precommit_bridge_handler import (
    AuditCodePrecommitBridgeHandler,
)
from fine_git_hooks.check_toolchains_precommit_bridge_handler import (
    CheckToolchainsPrecommitBridgeHandler,
)
from fine_git_hooks.format_precommit_bridge_handler import (
    FormatPrecommitBridgeHandler,
)
from fine_git_hooks.inspect_code_precommit_bridge_handler import (
    InspectCodePrecommitBridgeHandler,
)
from fine_git_hooks.type_check_precommit_bridge_handler import (
    TypeCheckPrecommitBridgeHandler,
)


class _FakeWorkspaceInfoProvider:
    def __init__(self, project_paths: list[pathlib.Path]) -> None:
        self._project_paths = project_paths

    async def get_workspace_projects(self) -> list[WorkspaceProject]:
        return [
            WorkspaceProject(path=path, config_status=ProjectConfigStatus.VALID)
            for path in self._project_paths
        ]


@dataclass
class _RecordedCall:
    action_type: type
    payload_by_project: dict[pathlib.Path, code_action.RunActionPayload] | None
    project_paths: list[pathlib.Path] | None


class _RecordingWorkspaceActionRunner:
    def __init__(
        self, results: dict[pathlib.Path, code_action.RunActionResult]
    ) -> None:
        self._results = results
        self.calls: list[_RecordedCall] = []

    async def run_action_per_project(
        self,
        action_type: type,
        payload_by_project: dict[pathlib.Path, code_action.RunActionPayload],
        meta: code_action.RunActionMeta,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, code_action.RunActionResult]:
        self.calls.append(
            _RecordedCall(
                action_type, payload_by_project=payload_by_project, project_paths=None
            )
        )
        return {
            path: self._results[path]
            for path in payload_by_project
            if path in self._results
        }

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, code_action.RunActionResult]:
        self.calls.append(
            _RecordedCall(
                action_type, payload_by_project=None, project_paths=project_paths
            )
        )
        assert project_paths is not None
        return {
            path: self._results[path] for path in project_paths if path in self._results
        }


class _RaisingWorkspaceActionRunner:
    async def run_action_per_project(
        self, action_type, payload_by_project, meta, concurrently=True
    ):
        raise iprojectactionrunner.ActionRunFailed("boom")

    async def run_action_in_projects(
        self, action_type, payload, meta, project_paths=None, concurrently=True
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


def _make_projects(tmp_path: pathlib.Path) -> list[pathlib.Path]:
    projects = [tmp_path / "one", tmp_path / "two", tmp_path / "three"]
    for project in projects:
        (project / "src").mkdir(parents=True)
    return projects


def _staged_files(projects: list[pathlib.Path]) -> list[pathlib.Path]:
    return [project / "src" / f"{project.name}.py" for project in projects]


def _expected_uris(projects: list[pathlib.Path]) -> list[list[str]]:
    return [
        [path_to_resource_uri(project / "src" / f"{project.name}.py")]
        for project in projects
    ]


_FILE_BRIDGE_CASES = [
    pytest.param(
        FormatPrecommitBridgeHandler,
        lambda: check_formatting_action.CheckFormattingRunResult(),
        FormatTarget.FILES,
        id="format",
    ),
    pytest.param(
        TypeCheckPrecommitBridgeHandler,
        lambda: TypeCheckRunResult(messages={}),
        TypeCheckTarget.FILES,
        id="type_check",
    ),
    pytest.param(
        InspectCodePrecommitBridgeHandler,
        lambda: InspectCodeRunResult(messages={}),
        InspectCodeTarget.FILES,
        id="inspect_code",
    ),
    pytest.param(
        AuditCodePrecommitBridgeHandler,
        lambda: AuditCodeRunResult(messages={}),
        AuditCodeTarget.FILES,
        id="audit_code",
    ),
]


@pytest.mark.parametrize(
    ("handler_cls", "result_factory", "expected_target"),
    _FILE_BRIDGE_CASES,
)
async def test_file_bridge_makes_one_call_with_per_project_payloads(
    tmp_path: pathlib.Path,
    handler_cls: type,
    result_factory,
    expected_target,
) -> None:
    projects = _make_projects(tmp_path)
    staged = _staged_files(projects)
    for path in staged:
        path.touch()

    runner = _RecordingWorkspaceActionRunner(
        {project: result_factory() for project in projects}
    )
    handler = handler_cls(
        workspace_action_runner=runner,
        workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
        logger=_FakeLogger(),
    )

    await handler.run(precommit_action.PrecommitRunPayload(), _run_context(staged))

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.payload_by_project is not None
    assert call.project_paths is None
    assert set(call.payload_by_project) == set(projects)
    for project, payload in call.payload_by_project.items():
        assert payload.target == expected_target
        assert payload.file_paths == [
            path_to_resource_uri(project / "src" / f"{project.name}.py")
        ]


async def test_check_toolchains_makes_one_projects_call_without_payloads(
    tmp_path: pathlib.Path,
) -> None:
    projects = _make_projects(tmp_path)
    staged = _staged_files(projects)
    for path in staged:
        path.touch()

    runner = _RecordingWorkspaceActionRunner(
        {project: CheckToolchainsRunResult() for project in projects}
    )
    handler = CheckToolchainsPrecommitBridgeHandler(
        workspace_action_runner=runner,
        workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
        logger=_FakeLogger(),
    )

    await handler.run(precommit_action.PrecommitRunPayload(), _run_context(staged))

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.payload_by_project is None
    assert call.project_paths == list(projects)


@pytest.mark.parametrize(
    ("handler_cls", "expected_message"),
    [
        pytest.param(
            TypeCheckPrecommitBridgeHandler,
            "Type check failed:\n  - boom",
            id="type_check",
        ),
        pytest.param(
            InspectCodePrecommitBridgeHandler,
            "Inspect code failed:\n  - boom",
            id="inspect_code",
        ),
        pytest.param(
            AuditCodePrecommitBridgeHandler,
            "Audit code failed:\n  - boom",
            id="audit_code",
        ),
        pytest.param(
            CheckToolchainsPrecommitBridgeHandler,
            "Toolchain check failed:\n  - boom",
            id="check_toolchains",
        ),
    ],
)
async def test_failure_keeps_existing_prefix_byte_for_byte(
    tmp_path: pathlib.Path, handler_cls: type, expected_message: str
) -> None:
    projects = _make_projects(tmp_path)
    staged = _staged_files(projects)
    for path in staged:
        path.touch()

    handler = handler_cls(
        workspace_action_runner=_RaisingWorkspaceActionRunner(),
        workspace_info_provider=_FakeWorkspaceInfoProvider(projects),
        logger=_FakeLogger(),
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await handler.run(precommit_action.PrecommitRunPayload(), _run_context(staged))

    assert str(exc_info.value) == expected_message
