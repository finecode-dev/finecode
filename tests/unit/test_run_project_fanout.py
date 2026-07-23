"""Fan-out width control on the `run` path (ADR-0067).

Two separate mechanisms, deliberately not interchangeable:

- `OrchestrationPolicy.max_project_fanout` is a *runaway-recursion* guard and
  refuses. It applies only to nested orchestration.
- A per-call semaphore is a *throttle* and never refuses. It applies to every
  fan-out, including the top-level one a person triggered.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest import mock

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.services.run_service import (
    proxy_utils,
    run_concurrency,
    workspace_executor,
)
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.execution_scopes import OrchestrationPolicy


def _actions_by_project(count: int) -> dict[pathlib.Path, list[str]]:
    return {pathlib.Path(f"/ws/project_{i}"): ["lint"] for i in range(count)}


async def test_wide_fanout_allowed_at_depth_zero() -> None:
    """A workspace simply containing more projects than the cap is not a
    runaway loop — refusing it made every workspace-wide action unusable past
    an arbitrary workspace size.
    """
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(10),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=0,
            policy=policy,
        )

    assert run_mock.await_count == 1


async def test_wide_fanout_refused_when_nested() -> None:
    """At depth > 0 the width was produced by a handler fanning out, which is
    exactly the blast radius ADR-0016 wants bounded."""
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with pytest.raises(ActionRunFailed) as exc_info:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(10),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=1,
            policy=policy,
        )

    assert "10" in str(exc_info.value)
    assert "depth 1" in str(exc_info.value)


async def test_narrow_fanout_allowed_when_nested() -> None:
    executor = workspace_executor.WorkspaceExecutor(
        context.WorkspaceContext(ws_dirs_paths=[pathlib.Path("/ws")])
    )
    policy = OrchestrationPolicy(max_project_fanout=4)

    with mock.patch.object(
        workspace_executor.proxy_utils, "run_actions_in_projects", return_value={}
    ) as run_mock:
        await executor.run_actions_in_projects(
            actions_by_project=_actions_by_project(3),
            params={},
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
            orchestration_depth=2,
            policy=policy,
        )

    assert run_mock.await_count == 1


def test_run_concurrency_default_is_sqrt_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This layer composes multiplicatively with the per-ER subprocess cap, so
    it takes the same sqrt-split default as ADR-0055's layers, not the full
    machine budget."""
    monkeypatch.delenv("FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS", raising=False)

    decision = run_concurrency.resolve_run_project_concurrency()

    assert decision.value == run_concurrency.default_layered_concurrency()
    assert "sqrt-split" in decision.source


def test_run_concurrency_env_var_overrides() -> None:
    with mock.patch.dict(
        "os.environ", {"FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS": "9"}
    ):
        decision = run_concurrency.resolve_run_project_concurrency()

    assert decision.value == 9
    assert "env var" in decision.source


def test_run_concurrency_zero_is_clamped_to_one() -> None:
    """A zero-sized limit would deadlock the fan-out forever rather than
    disable it — same clamp as ADR-0055's layers."""
    with mock.patch.dict(
        "os.environ", {"FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS": "0"}
    ):
        assert run_concurrency.resolve_run_project_concurrency().value == 1


def _resolved_project(dir_path: pathlib.Path) -> domain.ResolvedProject:
    return domain.ResolvedProject(
        name=dir_path.name,
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


async def test_fanout_is_throttled_not_refused(tmp_path: pathlib.Path) -> None:
    """The throttle must actually limit how many projects execute at once —
    without it a 67-project workspace put 67 ERs to work simultaneously, each
    free to spawn subprocesses up to its own cap — while still running all of
    them, never refusing.
    """
    cap = 3
    project_count = 12

    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    actions_by_project: dict[pathlib.Path, list[str]] = {}
    for i in range(project_count):
        project_dir = tmp_path / f"project_{i}"
        project_dir.mkdir()
        ws_context.ws_projects[project_dir] = _resolved_project(project_dir)
        actions_by_project[project_dir] = ["lint"]

    current = 0
    max_observed = 0
    completed: list[pathlib.Path] = []

    async def _fake_run_in_project(*, project, **_kwargs):
        nonlocal current, max_observed
        current += 1
        max_observed = max(max_observed, current)
        try:
            await asyncio.sleep(0.01)
        finally:
            current -= 1
        completed.append(project.dir_path)
        return {}

    with (
        mock.patch.dict(
            "os.environ", {"FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS": str(cap)}
        ),
        mock.patch.object(
            proxy_utils, "run_actions_in_running_project", _fake_run_in_project
        ),
    ):
        await proxy_utils.run_actions_in_projects(
            actions_by_project=actions_by_project,
            action_payload={},
            ws_context=ws_context,
            concurrently=True,
            result_formats=[proxy_utils.RunResultFormat.JSON],
            run_trigger=mock.Mock(),
            dev_env=mock.Mock(),
        )

    assert max_observed == cap
    # Throttled, not dropped: every project still ran.
    assert len(completed) == project_count
