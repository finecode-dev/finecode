from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
