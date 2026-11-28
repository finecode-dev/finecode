"""The delegated-calls read belongs to the handler's own action run.

A handler reads, from the run context it was given, whether every delegated
call of its own action run ended stopped. Same-env child runs count in their
caller's run at any depth; nothing of the run that started it or of a sibling
does. A call made from a task that carries no run, or after the run ended, is
counted nowhere.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import pathlib
import typing

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iworkspaceactionrunner,
)

from finecode_extension_runner import delegation_ledger, er_errors, er_server
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.impls import (
    project_action_runner,
    workspace_action_runner,
)
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _Payload(code_action.RunActionPayload):
    marker: str = ""


@dataclasses.dataclass
class _Result(code_action.RunActionResult):
    text: str = ""
    read: str = ""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _Result):
            if other.text:
                self.text = other.text
            if other.read:
                self.read = other.read


class _LeafAction(code_action.Action[_Payload, code_action.RunActionContext, _Result]):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result


class _ParentAction(
    code_action.Action[_Payload, code_action.RunActionContext, _Result]
):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result


class _ConcurrentParentAction(
    code_action.Action[_Payload, code_action.RunActionContext, _Result]
):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT


_WM_TARGET = "wm_target_action"

# Stub control: actionSource -> ("reply", stopped) | ("abandon", stopped) | ("block", None).
_STUBS: dict[str, tuple[str, bool | None]] = {}
_BLOCK_RELEASED = asyncio.Event()
_CAPTURED: dict[str, str] = {}


async def _stub_send(method: str, params: dict[str, typing.Any]) -> typing.Any:
    _ = method
    src = params.get("actionSource", "")
    kind, stopped = _STUBS.get(src, ("reply", True))
    if kind == "block":
        await _BLOCK_RELEASED.wait()
        return {"result": {"text": "ok"}, "subtreeStopped": True}
    if kind == "abandon":
        raise er_errors.WmCallAbandoned("gone", subtree_stopped=bool(stopped))
    return {"result": {"text": "ok"}, "subtreeStopped": bool(stopped)}


def _wm_ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef(
        source=_WM_TARGET, result_type=_Result, action_type=None
    )


def _leaf_ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef.from_type(_LeafAction)


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.USER,
        dev_env=code_action.DevEnv.CI,
    )


async def _leaf_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    return _Result(text="leaf", read=run_context.delegated_calls_outcome().value)


async def _parent_delegates_child_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    return _Result(text="parent", read=run_context.delegated_calls_outcome().value)


async def _parent_delegates_child_iter_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    async for _ in action_runner.run_action_iter(
        _leaf_ref(), _Payload(), run_context.meta
    ):
        pass
    return _Result(text="parent", read=run_context.delegated_calls_outcome().value)


async def _parent_pending_child_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    """Read while the same-env child still runs: the child counts as pending."""
    _ = payload
    task = asyncio.ensure_future(
        action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    )
    await asyncio.sleep(0.05)
    read_while_running = run_context.delegated_calls_outcome().value
    await task
    read_after = run_context.delegated_calls_outcome().value
    return _Result(text=read_while_running, read=read_after)


async def _leaf_blocking_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    return _Result(text="leaf")


async def _parent_sibling_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    """Child runs read STOPPED while a parent or sibling call is outstanding."""
    _ = payload
    await action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    sibling_read = _CAPTURED.get("sibling", "")
    return _Result(text=sibling_read, read=run_context.delegated_calls_outcome().value)


async def _sibling_reader_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
) -> _Result:
    _ = payload
    return _Result(text=run_context.delegated_calls_outcome().value)


async def _parent_with_pending_wm_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    """A same-env child reads STOPPED while the parent's own call is pending."""
    _ = payload
    pending = asyncio.ensure_future(
        action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    )
    await asyncio.sleep(0.05)
    child_result = await action_runner.run_action(
        _leaf_ref(), _Payload(), run_context.meta
    )
    _CAPTURED["child_while_parent_pending"] = child_result.text
    _BLOCK_RELEASED.set()
    await pending
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _child_reads_own_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
) -> _Result:
    _ = payload
    return _Result(text=run_context.delegated_calls_outcome().value)


async def _sequential_caller_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _concurrent_caller_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _task_caller_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload

    async def _do_call() -> None:
        await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)

    task = asyncio.ensure_future(_do_call())
    await task
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _workspace_caller_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    workspace_runner: iworkspaceactionrunner.IWorkspaceActionRunner,
) -> _Result:
    _ = payload
    try:
        await workspace_runner.run_action_in_projects(
            _LeafAction, _Payload(), run_context.meta
        )
    except iprojectactionrunner.ActionRunFailed:
        pass
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _failing_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    raise ValueError("child already unconfirmed; handler fails")


async def _cancelling_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    raise code_action.ActionCancelledException("stop after unconfirmed call")


def _actions_for(handler: typing.Any, action: type) -> dict[str, dict]:
    return {
        action.__name__: {
            "source": f"{action.__module__}.{action.__qualname__}",
            "handlers": [
                {
                    "name": "h",
                    "source": f"{handler.__module__}.{handler.__qualname__}",
                    "env": "test",
                }
            ],
        }
    }


def _actions_for_two(
    parent_handler: typing.Any,
    parent_action: type,
    child_handler: typing.Any,
    child_action: type,
) -> dict[str, dict]:
    actions = _actions_for(parent_handler, parent_action)
    actions.update(_actions_for(child_handler, child_action))
    return actions


async def _a29_recording_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _a29_fresh_reader_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
) -> _Result:
    _ = payload
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _a30_pending_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    task = asyncio.ensure_future(
        action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)
    )
    await asyncio.sleep(0.05)
    read = run_context.delegated_calls_outcome().value
    task.cancel()
    _BLOCK_RELEASED.set()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return _Result(read=read)


_A31_RETAINED: dict[str, typing.Any] = {}
_A31_RELEASE: asyncio.Event | None = None


async def _a31_late_parent_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    _A31_RETAINED["ctx"] = run_context

    async def _late() -> None:
        assert _A31_RELEASE is not None
        await _A31_RELEASE.wait()
        await action_runner.run_action(_wm_ref(), _Payload(), run_context.meta)

    _A31_RETAINED["task"] = asyncio.ensure_future(_late())
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _a28_top_parent_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _Result:
    _ = payload
    await action_runner.run_action(_leaf_ref(), _Payload(), run_context.meta)
    sibling = await action_runner.run_action(
        iprojectactionrunner.ActionRef.from_type(_ParentAction),
        _Payload(),
        run_context.meta,
    )
    return _Result(text=sibling.text, read=run_context.delegated_calls_outcome().value)


class TopAction(code_action.Action[_Payload, code_action.RunActionContext, _Result]):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result


class _ExtraDep:
    def __init__(self) -> None:
        self.tag = "extra"


class _CustomContext(code_action.RunActionContext[_Payload]):
    def __init__(
        self,
        run_id: int,
        initial_payload: _Payload,
        meta: code_action.RunActionMeta,
        info_provider: code_action.RunContextInfoProvider,
        extra: _ExtraDep,
    ) -> None:
        super().__init__(
            run_id=run_id,
            initial_payload=initial_payload,
            meta=meta,
            info_provider=info_provider,
        )
        self.extra = extra


class _CustomAction(code_action.Action[_Payload, _CustomContext, _Result]):
    PAYLOAD_TYPE = _Payload
    RUN_CONTEXT_TYPE = _CustomContext
    RESULT_TYPE = _Result


async def _a32_custom_handler(
    payload: _Payload,
    run_context: _CustomContext,
) -> _Result:
    _ = payload
    assert run_context.extra.tag == "extra"
    return _Result(read=run_context.delegated_calls_outcome().value)


class _PlainAction(code_action.Action[_Payload, code_action.RunActionContext, _Result]):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result


async def _a32_plain_handler(
    payload: _Payload,
    run_context: code_action.RunActionContext,
) -> _Result:
    _ = payload
    assert type(run_context) is code_action.RunActionContext
    return _Result(read=run_context.delegated_calls_outcome().value)


async def _install_stub_runners(session: typing.Any) -> None:
    ctx = session._runner_context
    proj_runner = project_action_runner.ProjectActionRunnerImpl(
        _stub_send,
        run_action_func=functools.partial(
            run_action_service.run_action, runner_context=ctx
        ),
        actions_getter=lambda: ctx.project.actions,
        current_env_name_getter=lambda: "test",
    )
    ctx.di_registry.register_instance(
        iprojectactionrunner.IProjectActionRunner, proj_runner, override=True
    )
    ws_runner = workspace_action_runner.WorkspaceActionRunnerImpl(_stub_send)
    ctx.di_registry.register_instance(
        iworkspaceactionrunner.IWorkspaceActionRunner, ws_runner, override=True
    )


def _reset_state() -> None:
    _STUBS.clear()
    _CAPTURED.clear()
    _BLOCK_RELEASED.clear()


async def test_a27_run_action_child_unconfirmed(tmp_path: pathlib.Path) -> None:
    """A same-env `run_action` child folds its unconfirmed call into the caller."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for_two(
        _parent_delegates_child_handler, _ParentAction, _leaf_handler, _LeafAction
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert result.read == "unconfirmed"


async def test_a27_run_action_iter_child_unconfirmed(tmp_path: pathlib.Path) -> None:
    """A same-env `run_action_iter` child folds its unconfirmed call as well."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for_two(
        _parent_delegates_child_iter_handler,
        _ParentAction,
        _leaf_handler,
        _LeafAction,
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert result.read == "unconfirmed"


async def test_a27_pending_child_and_all_stopped(tmp_path: pathlib.Path) -> None:
    """A still-running child counts as pending; all stopped reads stopped."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for_two(
        _parent_pending_child_handler,
        _ParentAction,
        _leaf_blocking_handler,
        _LeafAction,
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert result.text == "unconfirmed"
    assert result.read == "unconfirmed"

    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", True)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert result.text == "stopped"
    assert result.read == "stopped"


async def test_a28_child_reads_stopped_while_parent_call_pending(
    tmp_path: pathlib.Path,
) -> None:
    """A child run reads STOPPED while a call made by its parent is pending."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("block", None)
    actions = _actions_for_two(
        _parent_with_pending_wm_handler,
        _ParentAction,
        _child_reads_own_handler,
        _LeafAction,
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert _CAPTURED["child_while_parent_pending"] == "stopped"


async def test_a28_sibling_unconfirmed_does_not_leak(tmp_path: pathlib.Path) -> None:
    """A run reads STOPPED while a sibling same-env run's call went unconfirmed."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)

    actions = _actions_for_two(
        _leaf_handler, _LeafAction, _sibling_reader_handler, _ParentAction
    )
    # Parent logic differs: run leaf (unconfirmed) then read own record in a
    # second same-env child. Build the parent action separately.
    parent_actions = {
        "TopAction": {
            "source": f"{__name__}.TopAction",
            "handlers": [
                {
                    "name": "h",
                    "source": (
                        f"{_a28_top_parent_handler.__module__}"
                        f".{_a28_top_parent_handler.__qualname__}"
                    ),
                    "env": "test",
                }
            ],
        },
        **actions,
    }

    async with handler_test_session(
        project_dir=tmp_path, actions=parent_actions
    ) as session:
        await _install_stub_runners(session)
        result = await session.run_action("TopAction", _Payload())
    assert result is not None
    assert result.text == "stopped"
    assert result.read == "unconfirmed"


async def test_a29_restored_segment_starts_empty_and_serializes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A restored segment starts with no calls; the record is never serialized."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for(_sequential_caller_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "unconfirmed"
        ctx = session._runner_context
        action_def = ctx.project.actions[_ParentAction.__name__]

        probe_ctx = code_action.RunActionContext(
            run_id=1,
            initial_payload=_Payload(),
            meta=_meta(),
            info_provider=code_action.RunContextInfoProvider(
                is_concurrent_execution=False
            ),
        )
        serialized = run_action_service._serialize_context(probe_ctx)
        assert serialized is None
        caller_serialized = run_action_service._serialize_caller_kwargs(
            code_action.CallerRunContextKwargs()
        )
        assert "ledger" not in str(caller_serialized).lower()
        assert "subtreeStopped" not in str(caller_serialized)
        assert "subtreeStopped" not in str(serialized)

        restored = code_action.RunActionContext(
            run_id=2,
            initial_payload=_Payload(),
            meta=_meta(),
            info_provider=code_action.RunContextInfoProvider(
                is_concurrent_execution=False
            ),
        )
        run_action_service._restore_context(restored, {})
        assert restored.delegated_calls_outcome() is (
            code_action.DelegatedCallsOutcome.UNCONFIRMED
        )
        _ = action_def


async def test_a29_second_segment_with_previous_context_reads_stopped(
    tmp_path: pathlib.Path,
) -> None:
    """A segment restoring state from a prior unconfirmed segment reads STOPPED."""

    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for(_a29_recording_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        first = await session.run_action(_ParentAction.__name__, _Payload())
        assert first is not None and first.read == "unconfirmed"

    _reset_state()
    actions2 = _actions_for(_a29_fresh_reader_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions2) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "stopped"


async def test_a30_call_shapes_count_in_the_run(tmp_path: pathlib.Path) -> None:
    """Sequential, concurrent-handler and handler-task calls count in the run."""
    for handler, action in (
        (_sequential_caller_handler, _ParentAction),
        (_concurrent_caller_handler, _ConcurrentParentAction),
        (_task_caller_handler, _ParentAction),
    ):
        _reset_state()
        _STUBS[_WM_TARGET] = ("reply", False)
        async with handler_test_session(
            project_dir=tmp_path, actions=_actions_for(handler, action)
        ) as session:
            await _install_stub_runners(session)
            result = await session.run_action(action.__name__, _Payload())
        assert result is not None, handler.__name__
        assert result.read == "unconfirmed", handler.__name__


async def test_a30_pending_call_reads_unconfirmed(tmp_path: pathlib.Path) -> None:
    """A call still pending makes the read UNCONFIRMED."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("block", None)

    actions = _actions_for(_a30_pending_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
    assert result is not None
    assert result.read == "unconfirmed"


async def test_a31_calls_without_a_run_are_counted_nowhere(
    tmp_path: pathlib.Path,
) -> None:
    """A worker task carrying no run is counted in neither the read nor reply."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", True)
    actions = _actions_for(_fresh_reader_handler_for_a31, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        ctx = session._runner_context
        runner = await ctx.di_registry.get_instance(
            iprojectactionrunner.IProjectActionRunner
        )

        started = asyncio.Event()
        release = asyncio.Event()

        async def _worker() -> None:
            started.set()
            await release.wait()
            await runner.run_action(_wm_ref(), _Payload(), _meta())

        worker = asyncio.ensure_future(_worker())
        await asyncio.wait_for(started.wait(), timeout=5.0)

        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "stopped"
        release.set()
        await asyncio.wait_for(worker, timeout=5.0)


async def _fresh_reader_handler_for_a31(
    payload: _Payload,
    run_context: code_action.RunActionContext,
) -> _Result:
    _ = payload
    return _Result(read=run_context.delegated_calls_outcome().value)


async def test_a31_post_run_call_changes_neither_read_nor_reply(
    tmp_path: pathlib.Path,
) -> None:
    """A call made after the run ended keeps the run's frozen verdict."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", True)
    _A31_RETAINED.clear()
    release = asyncio.Event()
    global _A31_RELEASE
    _A31_RELEASE = release

    actions = _actions_for(_a31_late_parent_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "stopped"
        release.set()
        await asyncio.wait_for(_A31_RETAINED["task"], timeout=5.0)
        assert (
            _A31_RETAINED["ctx"].delegated_calls_outcome()
            is code_action.DelegatedCallsOutcome.STOPPED
        )


async def test_a32_context_outside_er_and_subclass_and_default(
    tmp_path: pathlib.Path,
) -> None:
    """Unset contexts read UNCONFIRMED; subclasses and defaults keep the read."""
    probe = code_action.RunActionContext(
        run_id=1,
        initial_payload=_Payload(),
        meta=_meta(),
        info_provider=code_action.RunContextInfoProvider(is_concurrent_execution=False),
    )
    assert probe.delegated_calls_outcome() is (
        code_action.DelegatedCallsOutcome.UNCONFIRMED
    )

    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    actions = _actions_for(_a32_custom_handler, _CustomAction)
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides={_ExtraDep: _ExtraDep()},
    ) as session:
        await _install_stub_runners(session)
        result = await session.run_action(_CustomAction.__name__, _Payload())
    assert result is not None
    assert result.read == "stopped"

    _reset_state()
    actions2 = _actions_for(_a32_plain_handler, _PlainAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions2) as session:
        await _install_stub_runners(session)
        result2 = await session.run_action(_PlainAction.__name__, _Payload())
    assert result2 is not None
    assert result2.read == "stopped"


async def test_a33_reply_verdict_covers_same_env_child(tmp_path: pathlib.Path) -> None:
    """Result, handler-error and cancelled replies carry the top-level verdict."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    top_actions = _actions_for_two(
        _parent_delegates_child_handler, _ParentAction, _leaf_handler, _LeafAction
    )
    async with handler_test_session(
        project_dir=tmp_path, actions=top_actions
    ) as session:
        await _install_stub_runners(session)
        server = er_server.ErServer()
        server._runner_context = session._runner_context  # type: ignore[assignment]
        server._wal_writer = None  # type: ignore[assignment]
        params = {
            "actionName": _ParentAction.__name__,
            "params": {},
            "options": {
                "runId": "run-a33",
                "meta": {"trigger": "system", "devEnv": "ci"},
            },
        }
        reply = await er_server.run_action(server, params)
        assert reply["subtreeStopped"] is False

    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", True)
    async with handler_test_session(
        project_dir=tmp_path, actions=top_actions
    ) as session:
        await _install_stub_runners(session)
        server = er_server.ErServer()
        server._runner_context = session._runner_context  # type: ignore[assignment]
        server._wal_writer = None  # type: ignore[assignment]
        params = {
            "actionName": _ParentAction.__name__,
            "params": {},
            "options": {
                "runId": "run-a33b",
                "meta": {"trigger": "system", "devEnv": "ci"},
            },
        }
        reply = await er_server.run_action(server, params)
        assert reply["subtreeStopped"] is True


async def test_a33_handler_error_reply_carries_verdict(tmp_path: pathlib.Path) -> None:
    """A handler-error reply still carries the unconfirmed verdict."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    err_actions = _actions_for_two(
        _failing_handler, _ParentAction, _leaf_handler, _LeafAction
    )
    async with handler_test_session(
        project_dir=tmp_path, actions=err_actions
    ) as session:
        await _install_stub_runners(session)
        server = er_server.ErServer()
        server._runner_context = session._runner_context  # type: ignore[assignment]
        server._wal_writer = None  # type: ignore[assignment]
        params = {
            "actionName": _ParentAction.__name__,
            "params": {},
            "options": {
                "runId": "run-a33err",
                "meta": {"trigger": "system", "devEnv": "ci"},
            },
        }
        reply = await er_server.run_action(server, params)
        assert reply["subtreeStopped"] is False
        assert "error" in reply


async def test_a33_cancelled_reply_carries_verdict(tmp_path: pathlib.Path) -> None:
    """A cancelled reply carries the unconfirmed verdict in error data."""
    _reset_state()
    _STUBS[_WM_TARGET] = ("reply", False)
    cancel_actions = _actions_for_two(
        _cancelling_handler, _ParentAction, _leaf_handler, _LeafAction
    )
    async with handler_test_session(
        project_dir=tmp_path, actions=cancel_actions
    ) as session:
        await _install_stub_runners(session)
        server = er_server.ErServer()
        server._runner_context = session._runner_context  # type: ignore[assignment]
        server._wal_writer = None  # type: ignore[assignment]
        params = {
            "actionName": _ParentAction.__name__,
            "params": {},
            "options": {
                "runId": "run-a33cancel",
                "meta": {"trigger": "system", "devEnv": "ci"},
            },
        }
        import finecode_jsonrpc

        with pytest.raises(finecode_jsonrpc.JsonRpcHandlerError) as exc_info:
            await er_server.run_action(server, params)
        assert exc_info.value.data == {"subtreeStopped": False}


async def test_a23_workspace_call_feeds_run_context_read(
    tmp_path: pathlib.Path,
) -> None:
    """Unconfirmed and stopped workspace calls feed the run context read."""
    _reset_state()
    _STUBS["__workspace__"] = ("reply", False)

    async def _ws_send(method: str, params: dict[str, typing.Any]) -> typing.Any:
        _ = method
        kind, stopped = _STUBS.get("__workspace__", ("reply", True))
        assert kind == "reply"
        if stopped:
            return {
                "subtreeStopped": True,
                "resultsByProject": {
                    str(tmp_path): {"entry": {"status": "ok", "result": {"text": "ok"}}}
                },
            }
        return {"subtreeStopped": False, "resultsByProject": {}}

    actions = _actions_for(_workspace_caller_handler, _ParentAction)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        ctx = session._runner_context
        proj_runner = project_action_runner.ProjectActionRunnerImpl(
            _stub_send,
            run_action_func=functools.partial(
                run_action_service.run_action, runner_context=ctx
            ),
            actions_getter=lambda: ctx.project.actions,
            current_env_name_getter=lambda: "test",
        )
        ctx.di_registry.register_instance(
            iprojectactionrunner.IProjectActionRunner, proj_runner, override=True
        )
        ctx.di_registry.register_instance(
            iworkspaceactionrunner.IWorkspaceActionRunner,
            workspace_action_runner.WorkspaceActionRunnerImpl(_ws_send),
            override=True,
        )
        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "unconfirmed"

    _STUBS["__workspace__"] = ("reply", True)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        ctx = session._runner_context
        proj_runner = project_action_runner.ProjectActionRunnerImpl(
            _stub_send,
            run_action_func=functools.partial(
                run_action_service.run_action, runner_context=ctx
            ),
            actions_getter=lambda: ctx.project.actions,
            current_env_name_getter=lambda: "test",
        )
        ctx.di_registry.register_instance(
            iprojectactionrunner.IProjectActionRunner, proj_runner, override=True
        )
        ctx.di_registry.register_instance(
            iworkspaceactionrunner.IWorkspaceActionRunner,
            workspace_action_runner.WorkspaceActionRunnerImpl(_ws_send),
            override=True,
        )
        result = await session.run_action(_ParentAction.__name__, _Payload())
        assert result is not None
        assert result.read == "stopped"
