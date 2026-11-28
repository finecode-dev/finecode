"""ER-side cancellation of delegated calls: send mode, bound, verdict.

REQUIREMENT: when a handler's await of a delegated call ends early, the child
run is cancelled; the parent's await then waits within the bound for the
child's acknowledgement and reads whether the subtree stopped.

Covers the ER-half acceptance rows: the send mode and its translation (P4.1),
the non-streamed and workspace paths (P4.2-P4.3), the streamed close (P4.4),
the same-env iterator (P4.5), and the bound (A21).
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import typing

import finecode_jsonrpc
import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from loguru import logger

from finecode_extension_runner import (
    context,
    delegation_ledger,
    domain,
    er_errors,
    er_server,
    logs,
    services,
)
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.impls import (
    _delegated_progress,
    project_action_runner,
    workspace_action_runner,
)


class _FakeSession:
    """Stands in for ``JsonRpcServerSession``; records ``send_request`` kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, typing.Any]] = []
        self.behavior: typing.Callable[..., typing.Awaitable[typing.Any]] | None = None

    async def send_request(
        self, method: str, params: typing.Any = None, **kwargs: typing.Any
    ) -> typing.Any:
        self.calls.append({"method": method, "params": params, **kwargs})
        if self.behavior is not None:
            return await self.behavior(method, params, **kwargs)
        return {"ok": True}


def _server_with_session() -> tuple[er_server.ErServer, _FakeSession]:
    server = er_server.ErServer()
    session = _FakeSession()
    server._session = session  # type: ignore[assignment]
    return server, session


async def test_run_methods_wait_for_reply_after_cancel_within_bound() -> None:
    """The run methods cancel on the WM but still await the child's reply."""
    server, session = _server_with_session()

    for method in (
        "finecode/runActionInProject",
        "finecode/runActionInWorkspace",
    ):
        await server.send_request_to_wm(method, {"a": 1})

    assert len(session.calls) == 2
    for call in session.calls:
        assert "cancel_on_abandon" not in call
        option = call["await_reply_after_cancel"]
        assert isinstance(option, finecode_jsonrpc.AwaitReplyAfterCancel)
        assert option.timeout == _delegated_progress.DELEGATED_STOP_WAIT_SECONDS


async def test_elicit_keeps_cancel_on_abandon_and_others_send_plain() -> None:
    """``finecode/elicit`` keeps its abandon mode; other methods send plain."""
    server, session = _server_with_session()

    await server.send_request_to_wm("finecode/elicit", {})
    await server.send_request_to_wm("projects/getRawConfig", {})

    assert session.calls[0]["cancel_on_abandon"] is True
    assert "await_reply_after_cancel" not in session.calls[0]
    assert "cancel_on_abandon" not in session.calls[1]
    assert "await_reply_after_cancel" not in session.calls[1]


async def test_send_mode_reads_bound_at_call_time(
    monkeypatch: typing.Any,
) -> None:
    """Tests can shrink the stop wait by patching the module constant."""
    monkeypatch.setattr(_delegated_progress, "DELEGATED_STOP_WAIT_SECONDS", 0.2)
    server, session = _server_with_session()

    await server.send_request_to_wm("finecode/runActionInProject", {})

    assert session.calls[0]["await_reply_after_cancel"].timeout == 0.2


def _cancelled_with_reply(
    reply: finecode_jsonrpc.ReplyAfterCancel | None,
) -> finecode_jsonrpc.RequestCancelledError:
    return finecode_jsonrpc.RequestCancelledError(request_id="req-1", reply=reply)


def _result_reply(stopped: bool) -> finecode_jsonrpc.ReplyAfterCancel:
    return finecode_jsonrpc.ReplyAfterCancel(
        kind=finecode_jsonrpc.ReplyAfterCancelKind.RESULT,
        result={"subtreeStopped": stopped},
    )


def _error_reply(stopped: object) -> finecode_jsonrpc.ReplyAfterCancel:
    data = None if stopped is None else {"subtreeStopped": stopped}
    return finecode_jsonrpc.ReplyAfterCancel(
        kind=finecode_jsonrpc.ReplyAfterCancelKind.ERROR,
        error_code=-32800,
        error_data=data,
    )


async def test_sender_translates_cancel_reply_into_abandoned_with_verdict() -> None:
    """A post-cancel RESULT/ERROR reply becomes ``WmCallAbandoned`` carrying it."""
    server, session = _server_with_session()
    sender = er_server.make_wm_request_sender(server)

    async def _raise_result(
        method: str, params: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        _ = (method, params, kwargs)
        raise _cancelled_with_reply(_result_reply(True))

    session.behavior = _raise_result
    try:
        await sender("finecode/runActionInProject", {})
    except er_errors.WmCallAbandoned as error:
        assert error.subtree_stopped is True
        assert isinstance(error.__cause__, finecode_jsonrpc.RequestCancelledError)
    else:
        raise AssertionError("expected the abandoned call")

    async def _raise_error(
        method: str, params: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        _ = (method, params, kwargs)
        raise _cancelled_with_reply(_error_reply(False))

    session.behavior = _raise_error
    try:
        await sender("finecode/runActionInWorkspace", {})
    except er_errors.WmCallAbandoned as error:
        assert error.subtree_stopped is False
    else:
        raise AssertionError("expected the abandoned call")


async def test_sender_treats_timeout_disconnect_and_missing_reply_as_unconfirmed() -> (
    None
):
    """TIMEOUT/DISCONNECTED (or no reply) abandon as unconfirmed, still chained."""
    server, session = _server_with_session()
    sender = er_server.make_wm_request_sender(server)

    for reply in (
        finecode_jsonrpc.ReplyAfterCancel(
            kind=finecode_jsonrpc.ReplyAfterCancelKind.TIMEOUT
        ),
        finecode_jsonrpc.ReplyAfterCancel(
            kind=finecode_jsonrpc.ReplyAfterCancelKind.DISCONNECTED
        ),
        None,
    ):

        async def _raise(
            method: str,
            params: typing.Any,
            _reply: finecode_jsonrpc.ReplyAfterCancel | None = reply,
            **kwargs: typing.Any,
        ) -> typing.Any:
            _ = (method, params, kwargs)
            raise _cancelled_with_reply(_reply)

        session.behavior = _raise
        try:
            await sender("finecode/runActionInProject", {})
        except er_errors.WmCallAbandoned as error:
            assert error.subtree_stopped is False
            assert isinstance(error.__cause__, finecode_jsonrpc.RequestCancelledError)
        else:
            raise AssertionError("expected the abandoned call")


async def test_abandoned_is_a_cancellation() -> None:
    """``WmCallAbandoned`` propagates through ``except CancelledError``."""
    error = er_errors.WmCallAbandoned("gone", subtree_stopped=True)
    assert isinstance(error, asyncio.CancelledError)

    seen: list[str] = []
    try:
        raise error
    except asyncio.CancelledError:
        seen.append("cancelled")
    assert seen == ["cancelled"]


async def test_stop_bound_within_design_maximum() -> None:
    """The stop wait fits inside the release wait and the 5.0 s design maximum."""
    assert (
        _delegated_progress.RELEASE_WAIT_SECONDS
        <= _delegated_progress.DELEGATED_STOP_WAIT_SECONDS
        <= 5.0
    )


@dataclasses.dataclass
class _Payload(code_action.RunActionPayload):
    marker: str = ""


@dataclasses.dataclass
class _Result(code_action.RunActionResult):
    text: str = ""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _Result):
            self.text = other.text


class _Action(code_action.Action[_Payload, code_action.RunActionContext, _Result]):
    PAYLOAD_TYPE = _Payload
    RESULT_TYPE = _Result


def _wm_path_runner(
    send: typing.Callable[[str, dict], typing.Awaitable[typing.Any]],
) -> project_action_runner.ProjectActionRunnerImpl:
    """Runner whose action lives in another env, so dispatch takes the WM path."""
    action_def = domain.ActionDeclaration(
        name="test_action",
        config={},
        handlers=[
            domain.ActionHandlerDeclaration(
                name="h", source="x.H", config={}, env="other_env"
            )
        ],
        source=f"{__name__}._Action",
    )

    async def _never_local(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (args, kwargs)
        raise AssertionError("expected the WM path, not the local one")

    return project_action_runner.ProjectActionRunnerImpl(
        send_request_to_wm=send,
        run_action_func=_never_local,
        actions_getter=lambda: {"test_action": action_def},
        current_env_name_getter=lambda: "dev_workspace",
    )


def _ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef(
        source=f"{__name__}._Action",
        result_type=_Result,
        action_type=_Action,
    )


def _meta() -> code_action.RunActionMeta:
    return code_action.RunActionMeta(
        trigger=code_action.RunActionTrigger.USER,
        dev_env=code_action.DevEnv.CI,
        orchestration_depth=0,
    )


async def test_nonstreamed_abandoned_call_ends_ledger_and_propagates_cancel() -> None:
    """An abandoned non-streamed await ends the call and raises CancelledError.

    ``WmCallAbandoned`` is a cancellation, never ``ActionRunCancelled``; the
    ledger records the verdict the abandoned wait observed.
    """
    for stopped, expected in ((True, "stopped"), (False, "unconfirmed")):

        async def _abandoned(
            method: str, params: dict, _stopped: bool = stopped
        ) -> typing.Any:
            _ = (method, params)
            raise er_errors.WmCallAbandoned("gone", subtree_stopped=_stopped)

        runner = _wm_path_runner(_abandoned)
        with delegation_ledger.bind_run() as ledger:
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await runner.run_action(_ref(), _Payload(), _meta())
            assert not isinstance(
                exc_info.value, iprojectactionrunner.ActionRunCancelled
            )
            assert ledger.outcome() == (
                code_action.DelegatedCallsOutcome.STOPPED
                if expected == "stopped"
                else code_action.DelegatedCallsOutcome.UNCONFIRMED
            )


async def test_nonstreamed_cancelled_reply_without_abandon_stays_action_cancelled() -> (
    None
):
    """A cancelled reply to a parent that was not cancelled stays mapped."""

    async def _cancelled(method: str, params: dict) -> typing.Any:
        _ = (method, params)
        raise er_errors.WmCommunicationCancelled("wm cancelled")

    runner = _wm_path_runner(_cancelled)
    with pytest.raises(iprojectactionrunner.ActionRunCancelled):
        await runner.run_action(_ref(), _Payload(), _meta())


def _ws_runner(
    send: typing.Callable[[str, dict], typing.Awaitable[typing.Any]],
) -> workspace_action_runner.WorkspaceActionRunnerImpl:
    return workspace_action_runner.WorkspaceActionRunnerImpl(send)


async def _call_workspace(
    runner: workspace_action_runner.WorkspaceActionRunnerImpl,
    method: str,
) -> typing.Any:
    if method == "run_action_in_projects":
        return await runner.run_action_in_projects(_Action, _Payload(), _meta())
    return await runner.run_action_per_project(
        _Action, {pathlib.Path("p1"): _Payload()}, _meta()
    )


async def test_workspace_abandoned_call_propagates_cancel_with_verdict() -> None:
    """An abandoned workspace await ends the call and raises CancelledError.

    Never ``ActionRunFailed``; the ledger records the abandoned verdict.
    """

    async def _never_called(method_name: str, params: dict) -> typing.Any:
        _ = (method_name, params)
        raise AssertionError("send must not be called for a bare read")

    project_runner = _wm_path_runner(_never_called)
    for method in ("run_action_in_projects", "run_action_per_project"):
        for stopped, expected in (
            (True, code_action.DelegatedCallsOutcome.STOPPED),
            (False, code_action.DelegatedCallsOutcome.UNCONFIRMED),
        ):

            async def _abandoned(
                method_name: str, params: dict, _stopped: bool = stopped
            ) -> typing.Any:
                _ = (method_name, params)
                raise er_errors.WmCallAbandoned("gone", subtree_stopped=_stopped)

            runner = _ws_runner(_abandoned)
            with delegation_ledger.bind_run() as ledger:
                with pytest.raises(asyncio.CancelledError) as exc_info:
                    await _call_workspace(runner, method)
                assert not isinstance(
                    exc_info.value, iprojectactionrunner.ActionRunFailed
                )
                assert ledger.outcome() == expected


async def test_workspace_cancelled_reply_maps_to_action_cancelled() -> None:
    """A WM-cancelled workspace call is a cancellation, never a failure."""

    async def _cancelled(method_name: str, params: dict) -> typing.Any:
        _ = (method_name, params)
        raise er_errors.WmCommunicationCancelled("wm cancelled")

    for method in ("run_action_in_projects", "run_action_per_project"):
        with pytest.raises(iprojectactionrunner.ActionRunCancelled):
            await _call_workspace(_ws_runner(_cancelled), method)


async def test_workspace_unconfirmed_feeds_project_read() -> None:
    """An unconfirmed workspace call makes the project read UNCONFIRMED."""

    async def _failed(method_name: str, params: dict) -> typing.Any:
        _ = (method_name, params)
        raise er_errors.WmCommunicationError("gone")

    ws_runner = _ws_runner(_failed)
    project_runner = _wm_path_runner(_failed)
    with delegation_ledger.bind_run() as ledger:
        with pytest.raises(iprojectactionrunner.ActionRunFailed):
            await ws_runner.run_action_in_projects(_Action, _Payload(), _meta())
        assert ledger.outcome() == code_action.DelegatedCallsOutcome.UNCONFIRMED


def _local_runner(
    handler_impl: typing.Any,
) -> project_action_runner.ProjectActionRunnerImpl:
    """Runner whose action lives in the current env (same-env path)."""
    action_def = domain.ActionDeclaration(
        name="test_action",
        config={},
        handlers=[
            domain.ActionHandlerDeclaration(
                name="h", source="x.H", config={}, env="dev_workspace"
            )
        ],
        source=f"{__name__}._Action",
    )

    async def _never_wm(method: str, params: dict) -> typing.Any:
        _ = (method, params)
        raise AssertionError("unexpected WM call on the same-env path")

    return project_action_runner.ProjectActionRunnerImpl(
        send_request_to_wm=_never_wm,
        run_action_func=handler_impl,
        actions_getter=lambda: {"test_action": action_def},
        current_env_name_getter=lambda: "dev_workspace",
    )


def _local_ref() -> iprojectactionrunner.ActionRef:
    return iprojectactionrunner.ActionRef(
        source=f"{__name__}._Action",
        result_type=_Result,
        action_type=_Action,
    )


async def test_same_env_stubborn_child_close_completes_at_bound_unconfirmed(
    monkeypatch: typing.Any,
) -> None:
    """A same-env child ignoring the cancel past the bound ends it unconfirmed.

    The close completes at the bound; the late failure is retrieved, never
    reported as unretrieved.
    """
    monkeypatch.setattr(_delegated_progress, "DELEGATED_STOP_WAIT_SECONDS", 0.3)
    started = asyncio.Event()
    children: list[asyncio.Task[typing.Any]] = []

    async def _stubborn(
        action_def: typing.Any,
        payload: typing.Any,
        meta: typing.Any,
        **kwargs: typing.Any,
    ) -> _Result:
        _ = (action_def, payload, meta)
        children.append(asyncio.current_task())
        queue: asyncio.Queue[_Result] = kwargs["partial_result_queue"]
        await queue.put(_Result(text="first"))
        started.set()
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError as exc:
            await asyncio.sleep(1.0)
            raise RuntimeError("late boom") from exc
        raise AssertionError("unreachable")

    runner = _local_runner(_stubborn)
    project_runner = _wm_path_runner(_stubborn)
    with delegation_ledger.bind_run() as ledger:

        async def _consume() -> list[_Result]:
            return [
                item
                async for item in runner.run_action_iter(
                    _local_ref(), _Payload(), _meta()
                )
            ]

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(started.wait(), timeout=5.0)
        task.cancel()
        start = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
        elapsed = asyncio.get_running_loop().time() - start
        assert elapsed < 0.3 + 0.5
        assert ledger.outcome() == code_action.DelegatedCallsOutcome.UNCONFIRMED
    await asyncio.sleep(1.2)
    assert len(children) == 1
    assert children[0].done()
    assert children[0]._log_traceback is False


async def test_same_env_prompt_cancel_counts_stopped() -> None:
    """A same-env child cancelled promptly counts the call stopped."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _compliant(
        action_def: typing.Any,
        payload: typing.Any,
        meta: typing.Any,
        **kwargs: typing.Any,
    ) -> _Result:
        _ = (action_def, payload, meta, kwargs)
        started.set()
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    runner = _local_runner(_compliant)
    project_runner = _wm_path_runner(_compliant)
    with delegation_ledger.bind_run() as ledger:

        async def _consume() -> list[_Result]:
            return [
                item
                async for item in runner.run_action_iter(
                    _local_ref(), _Payload(), _meta()
                )
            ]

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(started.wait(), timeout=5.0)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
        assert cancelled.is_set()
        assert ledger.outcome() == code_action.DelegatedCallsOutcome.STOPPED


async def test_same_env_normal_end_counts_stopped() -> None:
    """A same-env child that runs to its end counts the call stopped."""

    async def _quick(
        action_def: typing.Any,
        payload: typing.Any,
        meta: typing.Any,
        **kwargs: typing.Any,
    ) -> _Result:
        _ = (action_def, payload, meta)
        queue: asyncio.Queue[_Result] = kwargs["partial_result_queue"]
        await queue.put(_Result(text="only"))
        return _Result(text="only")

    runner = _local_runner(_quick)
    project_runner = _wm_path_runner(_quick)
    with delegation_ledger.bind_run() as ledger:
        got = [
            item
            async for item in runner.run_action_iter(_local_ref(), _Payload(), _meta())
        ]
        assert [r.text for r in got] == ["only"]
        assert ledger.outcome() == code_action.DelegatedCallsOutcome.STOPPED


async def test_same_env_second_cancel_does_not_shorten_wait() -> None:
    """A repeated cancellation during a same-env close never shortens it."""
    entered = asyncio.Event()
    release_gate = asyncio.Event()

    async def _gated(
        action_def: typing.Any,
        payload: typing.Any,
        meta: typing.Any,
        **kwargs: typing.Any,
    ) -> _Result:
        _ = (action_def, payload, meta, kwargs)
        entered.set()
        try:
            await release_gate.wait()
            return _Result(text="done")
        except asyncio.CancelledError:
            await release_gate.wait()
            raise

    runner = _local_runner(_gated)
    project_runner = _wm_path_runner(_gated)
    with delegation_ledger.bind_run() as ledger:

        async def _consume() -> list[_Result]:
            return [
                item
                async for item in runner.run_action_iter(
                    _local_ref(), _Payload(), _meta()
                )
            ]

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()
        release_gate.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected CancelledError")
        assert ledger.outcome() == code_action.DelegatedCallsOutcome.STOPPED


async def test_race_between_cancel_and_reply_ends_with_reply_verdict() -> None:
    """A cancel landing as the reply resolves ends with the reply's verdict.

    No ``$/cancelRequest`` goes out for an already-resolved reply; the serving
    layer answers the call cancelled with the verdict, and nothing above
    DEBUG is logged.
    """
    response = {"result": {"text": "hi"}, "subtreeStopped": True}
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        server, session = _server_with_session()

        async def _race(
            method: str, params: typing.Any = None, **kwargs: typing.Any
        ) -> typing.Any:
            _ = params
            assert kwargs.get("await_reply_after_cancel") is not None
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            raise finecode_jsonrpc.RequestCancelledError(
                request_id="race-1",
                reply=finecode_jsonrpc.ReplyAfterCancel(
                    kind=finecode_jsonrpc.ReplyAfterCancelKind.RESULT,
                    result=response,
                ),
            )

        session.behavior = _race
        sender = er_server.make_wm_request_sender(server)
        runner = _wm_path_runner(sender)
        with delegation_ledger.bind_run() as ledger:
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await runner.run_action(_ref(), _Payload(), _meta())
            assert not isinstance(
                exc_info.value, iprojectactionrunner.ActionRunCancelled
            )
            assert ledger.outcome() == code_action.DelegatedCallsOutcome.STOPPED
    finally:
        logger.remove(handler_id)
    assert [call["method"] for call in session.calls] == ["finecode/runActionInProject"]
    assert seen == []


async def test_old_wm_without_cancel_hook_times_out_unconfirmed(
    monkeypatch: typing.Any,
) -> None:
    """Against a WM that ignores the cancel, the await ends within the bound.

    The child runs to its end unobserved; the abandonment reads UNCONFIRMED
    and the close stays within the bound.
    """
    monkeypatch.setattr(_delegated_progress, "DELEGATED_STOP_WAIT_SECONDS", 0.3)
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        server, session = _server_with_session()

        async def _old_wm(
            method: str, params: typing.Any = None, **kwargs: typing.Any
        ) -> typing.Any:
            _ = params
            option = kwargs.get("await_reply_after_cancel")
            assert option is not None
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError as exc:
                # The old WM ignores the cancel: no reply ever arrives.
                await asyncio.sleep(option.timeout)
                raise finecode_jsonrpc.RequestCancelledError(
                    request_id="old-1",
                    reply=finecode_jsonrpc.ReplyAfterCancel(
                        kind=finecode_jsonrpc.ReplyAfterCancelKind.TIMEOUT,
                    ),
                ) from exc
            raise AssertionError("unreachable")

        session.behavior = _old_wm
        sender = er_server.make_wm_request_sender(server)
        runner = _wm_path_runner(sender)
        with delegation_ledger.bind_run() as ledger:
            start = asyncio.get_running_loop().time()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                task = asyncio.ensure_future(
                    runner.run_action(_ref(), _Payload(), _meta())
                )
                await asyncio.sleep(0.1)
                task.cancel()
                await task
            elapsed = asyncio.get_running_loop().time() - start
            assert 0.3 <= elapsed < 0.3 + 0.5
            assert not isinstance(
                exc_info.value, iprojectactionrunner.ActionRunCancelled
            )
            assert ledger.outcome() == code_action.DelegatedCallsOutcome.UNCONFIRMED
    finally:
        logger.remove(handler_id)
    assert [call["method"] for call in session.calls] == ["finecode/runActionInProject"]
    assert seen == []


def _er_handler_server(
    tmp_path: pathlib.Path,
) -> typing.Any:
    project = domain.Project(
        name="test_project",
        dir_path=tmp_path,
        def_path=tmp_path / "pyproject.toml",
        actions={},
        action_handler_configs={},
    )
    runner_context = context.RunnerContext(project=project, di_registry=Registry())
    server = er_server.ErServer()
    server._runner_context = runner_context
    server._wal_writer = None
    return server


def _er_run_params() -> dict[str, typing.Any]:
    return {
        "actionName": "some_action",
        "params": {},
        "options": {
            "runId": "test-run-id",
            "meta": {"trigger": "system", "devEnv": "ci"},
        },
    }


def _er_run_handlers_params() -> dict[str, typing.Any]:
    return {
        "actionName": "some_action",
        "handlerNames": ["h"],
        "params": {},
        "options": {
            "runId": "test-run-id",
            "meta": {"trigger": "system", "devEnv": "ci"},
        },
    }


async def test_child_cancelled_reply_carries_subtree_stopped(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    """A cancelled child ER reply carries the subtree verdict in error data."""

    async def _cancelled_with_unconfirmed_child(
        *args: typing.Any, **kwargs: typing.Any
    ) -> typing.Any:
        _ = args
        ledgers = kwargs.get("ledger_out")
        with delegation_ledger.bind_run() as record:
            if ledgers is not None:
                ledgers.append(record)
            call = record.open_call()
            call.end(stopped=False)
            raise asyncio.CancelledError()

    monkeypatch.setattr(services, "run_action_raw", _cancelled_with_unconfirmed_child)
    server = _er_handler_server(tmp_path)
    with pytest.raises(finecode_jsonrpc.JsonRpcHandlerError) as exc_info:
        await er_server.run_action(server, _er_run_params())
    assert exc_info.value.code == finecode_jsonrpc.REQUEST_CANCELLED
    assert exc_info.value.data == {"subtreeStopped": False}

    async def _cancelled_clean(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (args, kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(services, "run_action_raw", _cancelled_clean)
    with pytest.raises(finecode_jsonrpc.JsonRpcHandlerError) as exc_info:
        await er_server.run_action(server, _er_run_params())
    assert exc_info.value.data == {"subtreeStopped": True}


async def test_child_handlers_cancelled_reply_carries_subtree_stopped(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    """The runHandlers hop carries the verdict on cancelled replies too."""

    async def _cancelled_clean(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        _ = (args, kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(services, "run_handlers_raw", _cancelled_clean)
    server = _er_handler_server(tmp_path)
    with pytest.raises(finecode_jsonrpc.JsonRpcHandlerError) as exc_info:
        await er_server.run_handlers(server, _er_run_handlers_params())
    assert exc_info.value.code == finecode_jsonrpc.REQUEST_CANCELLED
    assert exc_info.value.data == {"subtreeStopped": True}


def test_not_started_cancel_data_registered_for_run_methods(
    tmp_path: pathlib.Path,
) -> None:
    """The serving session answers not-started run calls with stopped=true."""
    _ = tmp_path
    prev_sender = logs._forward_sender
    try:
        server = er_server.create_er_server()
        data = server._session._request_not_started_data
    finally:
        logs.set_forward_sender(prev_sender)
    assert data["actions/run"] == {"subtreeStopped": True}
    assert data["actions/runHandlers"] == {"subtreeStopped": True}
