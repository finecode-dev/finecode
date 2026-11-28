"""Cross-action coverage propagation through the run-scoped sink.

A bridge handler that builds a fresh result object from a sub-action's
selected fields performs no ``update()``, so the ``update()`` join cannot carry
a miss across that boundary. These tests pin the second choke point: the
action-runner impls deposit every sub-result's coverage into the run's sink,
and the run loop folds it into the run's result.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iworkspaceactionrunner,
)
from finecode_extension_api.resource_uri import ResourceUri

from finecode_extension_runner import coverage_sink
from finecode_extension_runner.impls import workspace_action_runner
from finecode_extension_runner.testing import handler_test_session

_ITEM_A = ResourceUri("file:///a.py")
_ITEM_B = ResourceUri("file:///b.toml")


@dataclasses.dataclass
class _SubPayload(code_action.RunActionPayload):
    items: list[ResourceUri] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _SubResult(code_action.RunActionResult):
    values: dict[str, list[str]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _ParentResult(code_action.RunActionResult):
    values: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    nested: list[code_action.RunActionResult] = dataclasses.field(default_factory=list)


class _SubAction(code_action.Action):
    RESULT_TYPE = _SubResult


class _ParentAction(code_action.Action):
    RESULT_TYPE = _ParentResult


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.SYSTEM,
        dev_env=code_action.DevEnv.CI,
    )


def _sub_ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef(
        source=f"{_SubAction.__module__}.{_SubAction.__qualname__}",
        result_type=_SubAction.RESULT_TYPE,
        action_type=_SubAction,
    )


class _SubHandler(
    code_action.ActionHandler[_SubAction, code_action.ActionHandlerConfig]
):
    """The sub-action: reports one miss per payload item, decides nothing."""

    async def run(
        self,
        payload: _SubPayload,
        run_context: code_action.RunActionContext,
    ) -> _SubResult:
        return _SubResult(
            values={},
            coverage=[
                ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=item)
                for item in payload.items
            ],
        )


class _SimpleBridgeHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Calls one sub-action and builds a fresh result from selected fields —
    the exact shape that defeats the ``update()`` join (the fine_format
    bridges)."""

    def __init__(self, runner: iprojectactionrunner.IProjectActionRunner):
        self._runner = runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        sub = await self._runner.run_action(
            _sub_ref(), _SubPayload(items=[_ITEM_A]), _meta()
        )
        return _ParentResult(values=dict(sub.values), nested=[sub])


class _TwoSubsBridgeHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Calls two sub-actions, each missing a different item, keeps both."""

    def __init__(self, runner: iprojectactionrunner.IProjectActionRunner):
        self._runner = runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        first = await self._runner.run_action(
            _sub_ref(), _SubPayload(items=[_ITEM_A]), _meta()
        )
        second = await self._runner.run_action(
            _sub_ref(), _SubPayload(items=[_ITEM_B]), _meta()
        )
        return _ParentResult(values={}, nested=[first, second])


class _TaskGroupBridgeHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Fans sub-action calls out over an ``asyncio.TaskGroup``, like
    ``lint_files`` — deposits happen inside child tasks and must reach the
    parent run's result."""

    def __init__(self, runner: iprojectactionrunner.IProjectActionRunner):
        self._runner = runner

    async def _run_child(self) -> None:
        await self._runner.run_action(_sub_ref(), _SubPayload(items=[_ITEM_B]), _meta())

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        async with asyncio.TaskGroup() as tg:
            for _ in range(3):
                tg.create_task(self._run_child())
        return _ParentResult(values={})


class _RebindingChildHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Children mutate a fresh sink of their own instead of the run's — the
    opposite of the correct contract, asserted as executable pin."""

    def __init__(self, runner: iprojectactionrunner.IProjectActionRunner):
        self._runner = runner

    async def _run_rebinding_child(self) -> None:
        token = coverage_sink.bind()
        try:
            await self._runner.run_action(
                _sub_ref(), _SubPayload(items=[_ITEM_B]), _meta()
            )
        finally:
            coverage_sink.unbind(token)

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        async with asyncio.TaskGroup() as tg:
            for _ in range(2):
                tg.create_task(self._run_rebinding_child())
        return _ParentResult(values={})


class _AbsorbingBridgeHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Calls a sub-action missing *ITEM_A* and *ITEM_B*, absorbs *ITEM_A*'s
    miss mid-run (the dump_config shape), then nests the sub-result inside its
    own result — the hard case: absorption must survive the nesting."""

    def __init__(self, runner: iprojectactionrunner.IProjectActionRunner):
        self._runner = runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        sub = await self._runner.run_action(
            _sub_ref(), _SubPayload(items=[_ITEM_A, _ITEM_B]), _meta()
        )
        coverage_sink.absorb_coverage([_ITEM_A])
        return _ParentResult(values={}, nested=[sub])


class _TwoTransportHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Same item arriving via both transports with different ranks must
    collapse to one entry at the higher rank — the sink union goes through
    ``merge_coverage``, not a second list concat."""

    def __init__(
        self,
        runner: iprojectactionrunner.IProjectActionRunner,
        ws_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    ):
        self._runner = runner
        self._ws_runner = ws_runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        await self._runner.run_action(_sub_ref(), _SubPayload(items=[_ITEM_A]), _meta())
        # Second transport: per-project dispatch with its own coveraged result.
        await self._ws_runner.run_action_per_project(
            _SubAction,
            {pathlib.Path("p1"): _SubPayload(items=[_ITEM_A])},
            _meta(),
        )
        return _ParentResult(values={})


class _WsHandler(
    code_action.ActionHandler[_ParentAction, code_action.ActionHandlerConfig]
):
    """Calls ``run_action_in_projects`` — the fan-out the motivating cases
    (inspect_code bridge, pre-commit bridge) actually take."""

    def __init__(
        self,
        ws_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
    ):
        self._ws_runner = ws_runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ParentAction.RUN_CONTEXT_TYPE,
    ) -> _ParentResult:
        results = await self._ws_runner.run_action_in_projects(
            _SubAction,
            _SubPayload(items=[_ITEM_B]),
            _meta(),
            project_paths=[pathlib.Path("p1")],
        )
        return _ParentResult(values={k: dict(v.values) for k, v in results.items()})


_ACTIONS = {
    "SubAction": {
        "source": f"{_SubAction.__module__}.{_SubAction.__qualname__}",
        "handlers": [
            {
                "name": "sub",
                "source": f"{_SubHandler.__module__}.{_SubHandler.__qualname__}",
                "env": "test",
            }
        ],
    }
}


def _parent_actions(handler_cls: type) -> dict[str, dict]:
    actions = {
        "ParentAction": {
            "source": f"{_ParentAction.__module__}.{_ParentAction.__qualname__}",
            "handlers": [
                {
                    "name": "parent",
                    "source": f"{handler_cls.__module__}.{handler_cls.__qualname__}",
                    "env": "test",
                }
            ],
        }
    }
    actions.update(_ACTIONS)
    return actions


async def test_bridge_without_forwarding_carries_the_miss(tmp_path: Path) -> None:
    """A handler calling a sub-action whose result carries a miss produces a
    parent result carrying that miss — the parent builds a fresh result object
    from selected fields and never forwards anything."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_SimpleBridgeHandler)
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_ITEM_A)
    ]


async def test_sibling_subactions_do_not_contaminate_each_other(
    tmp_path: Path,
) -> None:
    """A miss produced by sub-action A must not appear on sibling sub-action
    B's own result, even though both reached the same parent run — per-run
    sink binding keeps attribution intact."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_TwoSubsBridgeHandler)
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert [e.item for e in result.nested[0].unhandled] == [_ITEM_A]
    assert [e.item for e in result.nested[1].unhandled] == [_ITEM_B]
    assert {e.item for e in result.unhandled} == {_ITEM_A, _ITEM_B}


async def test_taskgroup_child_deposits_reach_the_parent_run(tmp_path: Path) -> None:
    """A miss deposited from inside an ``asyncio.TaskGroup`` child task — the
    ``lint_files`` fan-out shape — reaches the parent run's result: the child
    mutates the shared sink object, and mutation is visible, unlike a
    rebind."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_TaskGroupBridgeHandler)
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_ITEM_B)
    ]


async def test_rebinding_child_deposit_is_lost(tmp_path: Path) -> None:
    """A child task that *rebinds* the contextvar writes into its own context
    copy and its deposit is lost — the hazard encoded as an executable
    statement rather than a docstring sentence. This is the line a future
    reader must not "simplify": removing the rebind makes the deposit visible
    again, but the *contract* is that children mutate, never rebind."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_RebindingChildHandler)
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert result.unhandled == []


async def test_concurrent_runs_do_not_see_each_others_coverage(
    tmp_path: Path,
) -> None:
    """Two runs executing concurrently in one ER must not see each other's
    coverage — the per-run bind, and the failure mode of a process-global
    sink: run A's misses would be unioned into run B's result."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_TwoSubsBridgeHandler)
    ) as session:
        results = await asyncio.gather(
            session.run_action("ParentAction"),
            session.run_action("ParentAction"),
        )
    assert all(isinstance(r, _ParentResult) for r in results)
    first, second = results
    assert {e.item for e in first.unhandled} == {_ITEM_A, _ITEM_B}
    assert {e.item for e in second.unhandled} == {_ITEM_A, _ITEM_B}
    assert first.unhandled == second.unhandled


def _fake_ws_runner(
    entry: _SubResult,
) -> iworkspaceactionrunner.IWorkspaceActionRunner:
    """A compat fake for ``finecode/runActionInWorkspace``: returns the canned
    per-project result with coverage inside the serialized dict, exactly like
    the WM would after running the sub-action in another ER."""

    async def _send(method: str, params: dict) -> dict:
        assert method == "finecode/runActionInWorkspace"
        project_paths = params.get("projectPaths") or list(
            params.get("payloadOverridesByProject", {}).keys()
        )
        results_by_project = {
            p: {
                "test": {
                    "status": "success",
                    "result": dataclasses.asdict(entry),
                }
            }
            for p in project_paths or [pathlib.Path("p1").as_posix()]
        }
        return {"resultsByProject": results_by_project}

    return workspace_action_runner.WorkspaceActionRunnerImpl(_send)


async def test_workspace_fanout_deposit_reaches_the_calling_run(tmp_path: Path) -> None:
    """A miss recorded by a sub-action reached through
    ``run_action_in_projects`` — the path the inspect_code and pre-commit
    bridges actually take — arrives in the calling run's result: coverage rode
    inside the serialized per-project result and the workspace-runner deposit
    picks it up on this side."""
    entry = _SubResult(
        values={},
        coverage=[ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_ITEM_B)],
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_parent_actions(_WsHandler),
        service_overrides={
            iworkspaceactionrunner.IWorkspaceActionRunner: _fake_ws_runner(entry)
        },
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_ITEM_B)
    ]


async def test_sink_union_is_the_rank_max_join(tmp_path: Path) -> None:
    """The same item arriving by both transports (the local fast path and the
    workspace per-project dispatch) with two different statuses collapses to
    exactly one entry at the higher rank — the sink union goes through
    ``merge_coverage``, never a second list concat. A full-triple dedupe
    would still violate what matters here: one entry, the more-specific
    miss."""
    entry = _SubResult(
        values={},
        coverage=[
            ItemCoverage(
                status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
                item=_ITEM_A,
                detail="toml",
            )
        ],
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_parent_actions(_TwoTransportHandler),
        service_overrides={
            iworkspaceactionrunner.IWorkspaceActionRunner: _fake_ws_runner(entry)
        },
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert len(result.unhandled) == 1
    entry = result.unhandled[0]
    assert entry.item == _ITEM_A
    assert entry.status is CoverageStatus.NO_SUBACTION_FOR_LANGUAGE


async def test_absorption_suppresses_its_own_miss_even_when_nested(
    tmp_path: Path,
) -> None:
    """A bridge that calls ``absorb_coverage`` suppresses the miss it absorbed
    and no other — asserted in the hard case: absorb *then* nest the absorbed
    sub-result inside the bridge's own result *then* read ``unhandled``.
    Absorption is a lattice status, so the rank-max read across the nesting
    suppresses the absorbed item while another item in the same tree is
    still reported."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_parent_actions(_AbsorbingBridgeHandler)
    ) as session:
        result = await session.run_action("ParentAction")
    assert isinstance(result, _ParentResult)
    assert [e.item for e in result.unhandled] == [_ITEM_B]
