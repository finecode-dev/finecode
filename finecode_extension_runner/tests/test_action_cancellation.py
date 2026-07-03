from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from loguru import logger

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilspclient, iprojectactionrunner
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session

pytestmark = pytest.mark.anyio


class _CancellationTestAction(code_action.Action):
    """Uses the base Action's default PAYLOAD_TYPE/RUN_CONTEXT_TYPE/RESULT_TYPE —
    these tests only care about how a handler's raised exception is classified,
    not about producing a real result."""


class _LspCancelledHandler(
    code_action.ActionHandler[_CancellationTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        raise ilspclient.LspRequestCancelledError("cancelled by pyrefly")


class _HandlerInitiatedCancelledHandler(
    code_action.ActionHandler[_CancellationTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        raise code_action.ActionCancelledException("not applicable here")


async def _raise_lsp_cancelled() -> None:
    raise ilspclient.LspRequestCancelledError("cancelled inside task group")


class _TaskGroupCancelledHandler(
    code_action.ActionHandler[_CancellationTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_raise_lsp_cancelled())
        return None  # pragma: no cover - unreachable, task group always raises


class _WmBackChannelCancelledHandler(
    code_action.ActionHandler[_CancellationTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        raise iprojectactionrunner.ActionRunCancelled("cancelled via WM back-channel")


def _single_handler_action(action_name: str, handler_cls: type) -> dict[str, dict]:
    handler_source = f"{handler_cls.__module__}.{handler_cls.__qualname__}"
    action_source = f"{_CancellationTestAction.__module__}.{_CancellationTestAction.__qualname__}"
    return {
        action_name: {
            "source": action_source,
            "handlers": [{"name": handler_cls.__name__, "source": handler_source}],
        }
    }


async def test_lsp_request_cancelled_error_is_classified_as_cancellation(
    tmp_path: Path,
) -> None:
    """A handler that surfaces a downstream LSP server's cancellation must be
    reported as a benign cancellation (ActionCancelledException), not logged
    or propagated as a crash — this is what lets the ER skip an ERROR-level
    trace for something the IDE user did nothing wrong to trigger (e.g. just
    opening a file while another request was in flight).
    """
    actions = _single_handler_action("lsp_cancel_action", _LspCancelledHandler)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionCancelledException) as exc_info:
            await session.run_action("lsp_cancel_action")

    assert "cancelled by pyrefly" in exc_info.value.message


async def test_handler_initiated_cancellation_is_classified_the_same_as_lsp_cancellation(
    tmp_path: Path,
) -> None:
    """A handler that cancels itself for its own reasons (unrelated to any LSP
    server) must be classified identically to an LSP-initiated cancellation —
    both are benign, non-error outcomes from the ER's perspective.
    """
    actions = _single_handler_action(
        "handler_cancel_action", _HandlerInitiatedCancelledHandler
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionCancelledException) as exc_info:
            await session.run_action("handler_cancel_action")

    assert "not applicable here" in exc_info.value.message


async def test_cancellation_raised_inside_a_task_group_is_still_recognized(
    tmp_path: Path,
) -> None:
    """A cancellation raised from a task spawned inside a handler's own
    ``asyncio.TaskGroup`` surfaces as a ``BaseExceptionGroup`` wrapping the
    cancellation — this must still be recognized as a cancellation rather
    than falling through to the generic failure path just because it is
    wrapped in a group.
    """
    actions = _single_handler_action("task_group_cancel_action", _TaskGroupCancelledHandler)
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionCancelledException):
            await session.run_action("task_group_cancel_action")


async def test_wm_back_channel_cancellation_is_recognized(
    tmp_path: Path,
) -> None:
    """A cancellation that crossed the multi-env WM back-channel (surfaced to
    the handler as ``iprojectactionrunner.ActionRunCancelled``) must be
    recognized by the same classification as a locally-raised cancellation.
    """
    actions = _single_handler_action(
        "wm_back_channel_cancel_action", _WmBackChannelCancelledHandler
    )
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        with pytest.raises(run_action_service.ActionCancelledException) as exc_info:
            await session.run_action("wm_back_channel_cancel_action")

    assert "cancelled via WM back-channel" in exc_info.value.message


class _ConcurrentSingleHandlerAction(code_action.Action):
    """HANDLER_EXECUTION=CONCURRENT routes even a single handler through
    run_action's TaskGroup-based "concurrent handlers" branch rather than
    the plain sequential loop — an action doesn't need multiple handlers to
    hit that branch, just this flag.
    """

    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT


class _ConcurrentLspCancelledHandler(
    code_action.ActionHandler[
        _ConcurrentSingleHandlerAction, code_action.ActionHandlerConfig
    ]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        raise ilspclient.LspRequestCancelledError("cancelled by pyrefly")


def _collect_error_logs() -> tuple[int, list[str]]:
    records: list[str] = []
    sink_id = logger.add(
        lambda message: records.append(message.record["message"]), level="ERROR"
    )
    return sink_id, records


async def test_cancellation_of_a_single_handler_run_concurrently_is_recognized(
    tmp_path: Path,
) -> None:
    """Regression test: a handler cancellation raised inside run_action's
    TaskGroup-based "concurrent handlers" branch (HANDLER_EXECUTION=CONCURRENT)
    must still be classified as ActionCancelledException, not silently
    converted into an ActionFailedException with a full "Unhandled exception"
    traceback logged for what is actually a benign cancellation.
    """
    actions = _single_handler_action(
        "concurrent_cancel_action", _ConcurrentLspCancelledHandler
    )
    sink_id, error_logs = _collect_error_logs()
    try:
        async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
            with pytest.raises(run_action_service.ActionCancelledException) as exc_info:
                await session.run_action("concurrent_cancel_action")
    finally:
        logger.remove(sink_id)

    assert "cancelled by pyrefly" in exc_info.value.message
    assert not any("Unhandled exception" in record for record in error_logs)


class _DispatchToConcurrentNestedActionHandler(
    code_action.ActionHandler[_CancellationTestAction, code_action.ActionHandlerConfig]
):
    """A handler that dispatches to a nested subaction from inside its own
    TaskGroup — this nests two TaskGroup boundaries around the eventual
    cancellation: the dispatching handler's own, and run_action's
    concurrent-handlers TaskGroup for the nested subaction.
    """

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner
    ) -> None:
        self.action_runner = action_runner

    async def _run_nested(
        self,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
    ) -> None:
        await self.action_runner.run_action(
            iprojectactionrunner.ActionRef.from_type(_ConcurrentSingleHandlerAction),
            payload,
            meta,
        )

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> code_action.RunActionResult:
        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._run_nested(payload, meta))
        return None  # pragma: no cover - unreachable, task group always raises


async def test_cancellation_propagates_through_a_dispatch_handlers_own_task_group(
    tmp_path: Path,
) -> None:
    """End-to-end regression test: a handler that dispatches to a nested
    subaction from inside its own TaskGroup, where the nested subaction's
    single handler runs CONCURRENT and gets cancelled. The cancellation must
    survive both TaskGroup boundaries as ActionCancelledException, with no
    "Unhandled exception in action handler" trace logged at either level.
    """
    dispatch_source = (
        f"{_CancellationTestAction.__module__}.{_CancellationTestAction.__qualname__}"
    )
    dispatch_handler_source = (
        f"{_DispatchToConcurrentNestedActionHandler.__module__}"
        f".{_DispatchToConcurrentNestedActionHandler.__qualname__}"
    )
    nested_source = (
        f"{_ConcurrentSingleHandlerAction.__module__}"
        f".{_ConcurrentSingleHandlerAction.__qualname__}"
    )
    nested_handler_source = (
        f"{_ConcurrentLspCancelledHandler.__module__}"
        f".{_ConcurrentLspCancelledHandler.__qualname__}"
    )
    actions = {
        "dispatch_action": {
            "source": dispatch_source,
            "handlers": [
                {"name": "dispatch", "source": dispatch_handler_source},
            ],
        },
        "concurrent_cancel_action": {
            "source": nested_source,
            "handlers": [
                # env must match handler_test_session's current env ("test")
                # so IProjectActionRunner takes the local fast path instead
                # of the WM round-trip — that fast path is what leaks the
                # unwrapped local run_action.ActionFailedException in the
                # original bug.
                {"name": "pyrefly", "source": nested_handler_source, "env": "test"},
            ],
        },
    }

    sink_id, error_logs = _collect_error_logs()
    try:
        async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
            with pytest.raises(run_action_service.ActionCancelledException) as exc_info:
                await session.run_action("dispatch_action")
    finally:
        logger.remove(sink_id)

    assert "cancelled by pyrefly" in exc_info.value.message
    assert not any("Unhandled exception" in record for record in error_logs)
