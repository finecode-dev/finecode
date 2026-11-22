"""The WM tells a client when a question stops waiting for it (ADR-0167, ADR-0168).

A question left on a person's screen after the WM has stopped waiting for it
either blocks them or, worse, takes the answer meant for the next question. So
every way the WM stops waiting must tell the client first, in an order the
client can rely on: the withdrawal is on the wire before the next question of
the same run, and before the response of the run that asked. An answer and a
withdrawal that cross never both take effect.
"""

from __future__ import annotations

import asyncio
import json
import typing

import pytest
from finecode_extension_runner.testing import run_on_virtual_time
from loguru import logger

from finecode.wm_server import wm_server
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_manager,
)
from finecode_jsonrpc import client as jsonrpc_client
from finecode_jsonrpc import error_codes

WITHDRAWN = "client/elicitWithdrawn"
ELICIT = "client/elicit"


class _FakeWriter:
    """Captures what the server writes; stands in for a client connection."""

    def __init__(self) -> None:
        self.closing = False

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    async def drain(self) -> None:
        return None


Message = dict[str, typing.Any]
Event = tuple[_FakeWriter, Message]


@pytest.fixture(autouse=True)
def events(monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    written: list[Event] = []

    def _capture(writer: typing.Any, msg: Message) -> None:
        written.append((writer, msg))

    monkeypatch.setattr(wm_server, "_write_message", _capture)
    return written


@pytest.fixture(autouse=True)
def clean_state() -> typing.Iterator[None]:
    yield
    wm_server._pending_client_requests.clear()
    wm_server._pending_client_request_owners.clear()
    wm_server._pending_questions.clear()
    wm_server._client_capabilities.clear()
    wm_server._connected_clients.clear()
    elicitation_bridge.reset_origins()


@pytest.fixture
def problems() -> typing.Iterator[list[str]]:
    """Warnings and errors the WM logged while a test ran."""
    seen: list[str] = []
    handler_id = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    yield seen
    logger.remove(handler_id)


def _client() -> _FakeWriter:
    writer = _FakeWriter()
    wm_server._connected_clients.add(writer)  # type: ignore[arg-type]
    wm_server._client_capabilities[writer] = {"elicitation": True}  # type: ignore[index]
    return writer


def _ask(
    writer: asyncio.StreamWriter | _FakeWriter | None,
    run_id: str | None,
    *,
    timeout_sec: float = 300.0,
) -> asyncio.Task[Message]:
    return asyncio.create_task(
        wm_server._WmElicitationBridge().elicit(
            message="keep it?",
            options=["keep", "revert"],
            default=None,
            timeout_sec=timeout_sec,
            run_writer_key=writer,  # type: ignore[arg-type]
            run_id=run_id,
        )
    )


def _methods(events: list[Event]) -> list[str]:
    return [msg.get("method", "response") for _, msg in events]


def _withdrawals(events: list[Event]) -> list[Message]:
    return [msg["params"] for _, msg in events if msg.get("method") == WITHDRAWN]


def _question_ids(events: list[Event]) -> list[int]:
    return [msg["id"] for _, msg in events if msg.get("method") == ELICIT]


def _answer(request_id: int, value: str = "keep") -> None:
    wm_server._resolve_client_response(
        request_id, {"result": {"outcome": "answered", "value": value}}
    )


# ---------------------------------------------------------------------------
# The deadline
# ---------------------------------------------------------------------------


def test_expiry_withdraws_the_question_once_and_returns_unavailable(
    events: list[Event], problems: list[str]
) -> None:
    async def scenario() -> None:
        writer = _client()
        asking = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)

        assert await asking == {"outcome": "unavailable"}

        assert _methods(events) == [ELICIT, WITHDRAWN]
        assert _withdrawals(events) == [{"id": question_id, "reason": "expired"}]

    run_on_virtual_time(scenario())
    assert problems == []


async def test_an_answer_before_the_deadline_sends_no_withdrawal(
    events: list[Event],
) -> None:
    writer = _client()
    asking = _ask(writer, "run-1")
    await asyncio.sleep(0)

    _answer(_question_ids(events)[0])

    assert await asking == {"outcome": "answered", "value": "keep"}
    assert _methods(events) == [ELICIT]


def test_an_answer_after_the_deadline_is_dropped_quietly(
    events: list[Event], problems: list[str]
) -> None:
    async def scenario() -> None:
        writer = _client()
        asking = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)
        assert await asking == {"outcome": "unavailable"}

        _answer(question_id)

        assert len(_withdrawals(events)) == 1

    run_on_virtual_time(scenario())
    assert problems == []


async def test_losing_the_client_sends_no_withdrawal(events: list[Event]) -> None:
    writer = _client()
    asking = _ask(writer, "run-1")
    await asyncio.sleep(0)

    wm_server._fail_pending_requests_for(writer)  # type: ignore[arg-type]

    assert await asking == {"outcome": "unavailable"}
    assert _methods(events) == [ELICIT]


def test_a_closing_connection_does_not_make_the_deadline_raise(
    events: list[Event], problems: list[str]
) -> None:
    async def scenario() -> None:
        writer = _client()
        asking = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)
        writer.closing = True

        assert await asking == {"outcome": "unavailable"}

        assert _withdrawals(events) == []

    run_on_virtual_time(scenario())
    assert problems == []


def test_a_failing_write_does_not_change_what_the_handler_is_told(
    monkeypatch: pytest.MonkeyPatch, events: list[Event]
) -> None:
    async def scenario() -> None:
        writer = _client()
        asking = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)

        def _broken(_writer: typing.Any, msg: Message) -> None:
            raise ConnectionResetError

        monkeypatch.setattr(wm_server, "_write_message", _broken)

        assert await asking == {"outcome": "unavailable"}

    run_on_virtual_time(scenario())


def test_the_next_question_of_a_run_follows_the_withdrawal_of_the_last(
    events: list[Event],
) -> None:
    async def scenario() -> None:
        writer = _client()
        first = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)
        assert await first == {"outcome": "unavailable"}
        second = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)

        assert _methods(events) == [ELICIT, WITHDRAWN, ELICIT]
        second.cancel()

    run_on_virtual_time(scenario())


# ---------------------------------------------------------------------------
# The end of the run that asked
# ---------------------------------------------------------------------------


def _bound(
    writer: _FakeWriter,
    run_id: str,
    request_task: asyncio.Task[typing.Any] | None = None,
) -> typing.ContextManager[None]:
    return elicitation_bridge.bind_run(
        run_id,
        elicitation_bridge.RunDispatchOrigin(
            connection=writer,  # type: ignore[arg-type]
            request_task=request_task,
        ),
    )


async def test_a_run_that_returns_withdraws_its_question_before_its_response(
    events: list[Event],
) -> None:
    writer = _client()
    with _bound(writer, "run-1"):
        asking = _ask(writer, "run-1")
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)
    events.append((writer, {"method": "response-of-the-run"}))

    assert await asyncio.wait_for(asking, timeout=0.5) == {"outcome": "unavailable"}
    assert _methods(events) == [ELICIT, WITHDRAWN, "response-of-the-run"]
    assert _withdrawals(events) == [{"id": question_id, "reason": "noLongerNeeded"}]


async def test_a_run_that_fails_withdraws_its_question(events: list[Event]) -> None:
    writer = _client()
    with pytest.raises(RuntimeError), _bound(writer, "run-1"):
        asking = _ask(writer, "run-1")
        await asyncio.sleep(0)
        raise RuntimeError("the run failed")

    assert await asyncio.wait_for(asking, timeout=0.5) == {"outcome": "unavailable"}
    assert [w["reason"] for w in _withdrawals(events)] == ["noLongerNeeded"]


async def test_cancelling_the_request_task_marks_its_run_cancelled(
    events: list[Event],
) -> None:
    writer = _client()
    request_task = asyncio.create_task(asyncio.sleep(3600))
    with _bound(writer, "run-1", request_task):
        asking = _ask(writer, "run-1")
        await asyncio.sleep(0)

        wm_server._cancel_client_request_task(request_task)

        assert [w["reason"] for w in _withdrawals(events)] == ["runCancelled"]
        assert await asyncio.wait_for(asking, timeout=0.5) == {"outcome": "unavailable"}
    assert [w["reason"] for w in _withdrawals(events)] == ["runCancelled"]
    with pytest.raises(asyncio.CancelledError):
        await request_task


def test_a_deadline_after_the_mark_is_runCancelled_and_before_it_is_expired(
    events: list[Event],
) -> None:
    async def scenario() -> None:
        writer = _client()
        request_task = asyncio.create_task(asyncio.sleep(3600))
        with _bound(writer, "run-1", request_task):
            expired_before = _ask(writer, "run-1", timeout_sec=5.0)
            await asyncio.sleep(0)
            await expired_before
            elicitation_bridge.mark_cancelled_by_wm(request_task)
            expired_after = _ask(writer, "run-1", timeout_sec=5.0)
            await asyncio.sleep(0)
            await expired_after
        request_task.cancel()

        assert [w["reason"] for w in _withdrawals(events)] == [
            "expired",
            "runCancelled",
        ]

    run_on_virtual_time(scenario())


async def test_a_branch_cancelled_by_a_sibling_failure_is_not_runCancelled(
    events: list[Event],
) -> None:
    writer = _client()
    asking: asyncio.Task[Message] | None = None

    async def branch() -> None:
        with _bound(writer, "run-1"):
            await asyncio.sleep(3600)

    async def failing_sibling() -> None:
        nonlocal asking
        asking = _ask(writer, "run-1")
        await asyncio.sleep(0)
        raise RuntimeError("sibling failed")

    with pytest.raises(ExceptionGroup):
        async with asyncio.TaskGroup() as group:
            group.create_task(branch())
            await asyncio.sleep(0)
            group.create_task(failing_sibling())

    assert asking is not None
    assert await asyncio.wait_for(asking, timeout=0.5) == {"outcome": "unavailable"}
    assert [w["reason"] for w in _withdrawals(events)] == ["noLongerNeeded"]


async def test_a_nested_run_keeps_its_question_when_the_root_is_cancelled(
    events: list[Event],
) -> None:
    writer = _client()
    request_task = asyncio.create_task(asyncio.sleep(3600))
    with _bound(writer, "root", request_task), _bound(writer, "nested"):
        asking = _ask(writer, "nested")
        await asyncio.sleep(0)

        wm_server._cancel_client_request_task(request_task)

        assert _withdrawals(events) == []
    assert [w["reason"] for w in _withdrawals(events)] == ["noLongerNeeded"]
    assert await asyncio.wait_for(asking, timeout=0.5) == {"outcome": "unavailable"}


async def test_an_answer_that_has_arrived_is_not_swept(events: list[Event]) -> None:
    writer = _client()
    with _bound(writer, "run-1"):
        asking = _ask(writer, "run-1")
        await asyncio.sleep(0)
        _answer(_question_ids(events)[0])

    assert await asking == {"outcome": "answered", "value": "keep"}
    assert _withdrawals(events) == []


async def test_a_question_asked_after_its_run_ended_is_never_sent(
    events: list[Event],
) -> None:
    writer = _client()
    with _bound(writer, "run-1"):
        pass

    origin = elicitation_bridge.originating_client_for_run("run-1")
    asking = _ask(origin, "run-1")

    assert await asking == {"outcome": "unavailable"}
    assert events == []


async def test_a_bridge_that_raises_at_run_end_does_not_change_the_run(
    events: list[Event], problems: list[str]
) -> None:
    class _Raising:
        def end_run(self, *, run_id: str) -> None:
            raise RuntimeError("broken bridge")

    writer = _client()
    elicitation_bridge.install(_Raising())  # type: ignore[arg-type]
    try:
        with _bound(writer, "run-1"):
            pass
    finally:
        elicitation_bridge.install(wm_server._WmElicitationBridge())

    assert any("broken bridge" in p for p in problems)


# ---------------------------------------------------------------------------
# The ER abandons its ask (driven through the real JSON-RPC client)
# ---------------------------------------------------------------------------


class _ErConnection:
    """The WM's JSON-RPC client for one ER, with the handlers ``runner_manager`` registers."""

    def __init__(self, *, with_cancel_hook: bool = True) -> None:
        self.client = jsonrpc_client.JsonRpcClient(
            message_types=_internal_client_types.METHOD_TO_TYPES,
            readable_id="test-er",
        )
        self.sent: list[Message] = []
        self.client._send_data = lambda data: self.sent.append(json.loads(data))  # type: ignore[method-assign]
        self.client.feature(
            _internal_client_types.ELICIT,
            runner_manager.handle_elicit,
            on_peer_cancel=(
                runner_manager.abandon_elicit if with_cancel_hook else None
            ),
        )

    async def ask(self, request_id: int, run_id: str | None) -> None:
        await self.client.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": _internal_client_types.ELICIT,
                "params": {
                    "message": "keep it?",
                    "options": ["keep", "revert"],
                    "default": None,
                    "timeoutSec": 300.0,
                    "runId": run_id,
                },
            }
        )

    async def cancel(self, request_id: int) -> None:
        await self.client.handle_message(
            {
                "jsonrpc": "2.0",
                "method": "$/cancelRequest",
                "params": {"id": request_id},
            }
        )

    async def settle(self) -> None:
        await asyncio.gather(*self.client._async_tasks, return_exceptions=True)


async def test_an_abandoned_ask_is_withdrawn_and_its_request_answered_cancelled(
    events: list[Event], problems: list[str]
) -> None:
    writer = _client()
    er = _ErConnection()
    with _bound(writer, "run-1"):
        await er.ask(7, "run-1")
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)

        await er.cancel(7)
        await er.settle()

    assert _withdrawals(events) == [{"id": question_id, "reason": "noLongerNeeded"}]
    assert [m["error"]["code"] for m in er.sent] == [
        error_codes.DEFAULT_REQUEST_CANCELLED
    ]
    assert er.client._async_tasks == []
    assert problems == []


async def test_the_withdrawal_of_an_abandoned_ask_precedes_the_next_question(
    events: list[Event],
) -> None:
    writer = _client()
    er = _ErConnection()
    with _bound(writer, "run-1"):
        await er.ask(7, "run-1")
        await asyncio.sleep(0)
        await er.cancel(7)
        await er.ask(8, "run-1")
        await asyncio.sleep(0)

        assert _methods(events) == [ELICIT, WITHDRAWN, ELICIT]
        await er.cancel(8)
        await er.settle()


async def test_an_abandoned_ask_of_a_marked_run_is_runCancelled(
    events: list[Event],
) -> None:
    writer = _client()
    er = _ErConnection()
    request_task = asyncio.create_task(asyncio.sleep(3600))
    with _bound(writer, "run-1", request_task):
        elicitation_bridge.mark_cancelled_by_wm(request_task)
        await er.ask(7, "run-1")
        await asyncio.sleep(0)

        await er.cancel(7)
        await er.settle()
    request_task.cancel()

    assert [w["reason"] for w in _withdrawals(events)] == ["runCancelled"]


async def test_cancelling_an_ask_before_it_started_sends_no_question(
    events: list[Event], problems: list[str]
) -> None:
    writer = _client()
    er = _ErConnection()
    with _bound(writer, "run-1"):
        await er.ask(7, "run-1")
        await er.cancel(7)
        await er.settle()

    assert events == []
    assert [m["error"]["code"] for m in er.sent] == [
        error_codes.DEFAULT_REQUEST_CANCELLED
    ]
    assert problems == []


async def test_repeated_unknown_and_late_cancels_withdraw_at_most_once(
    events: list[Event], problems: list[str]
) -> None:
    writer = _client()
    er = _ErConnection()
    with _bound(writer, "run-1"):
        await er.ask(7, "run-1")
        await asyncio.sleep(0)
        await er.cancel(7)
        await er.cancel(7)
        await er.cancel(99)
        await er.settle()
        await er.cancel(7)

        await er.ask(8, "run-1")
        await asyncio.sleep(0)
        _answer(_question_ids(events)[-1])
        await er.cancel(8)
        await er.settle()

    assert len(_withdrawals(events)) == 1
    assert problems == []


async def test_a_cancel_for_another_request_leaves_it_being_served() -> None:
    served = asyncio.Event()
    release = asyncio.Event()

    async def run_action(_params: typing.Any) -> None:
        served.set()
        await release.wait()

    er = _ErConnection()
    method = _internal_client_types.RUN_ACTION_IN_PROJECT
    er.client.feature(method, run_action)
    await er.client.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": method,
            "params": {
                "actionSource": "pkg.SomeAction",
                "payload": {},
                "meta": {"trigger": "user", "devEnv": "cli"},
            },
        }
    )
    await asyncio.wait_for(served.wait(), timeout=2.0)

    await er.cancel(5)
    release.set()
    await er.settle()

    assert [("result" in m) for m in er.sent] == [True]


# ---------------------------------------------------------------------------
# Older peers
# ---------------------------------------------------------------------------


def test_a_client_that_ignores_withdrawals_sees_one_extra_frame_and_nothing_else(
    events: list[Event], problems: list[str]
) -> None:
    async def scenario() -> None:
        writer = _client()
        asking = _ask(writer, "run-1", timeout_sec=5.0)
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)
        assert await asking == {"outcome": "unavailable"}

        _answer(question_id)

        assert _methods(events) == [ELICIT, WITHDRAWN]

    run_on_virtual_time(scenario())
    assert problems == []


async def test_an_older_wm_serves_a_cancelled_ask_to_its_end(
    events: list[Event], problems: list[str]
) -> None:
    """A WM that registered no cancel hook keeps the question open, as it always did."""
    writer = _client()
    er = _ErConnection(with_cancel_hook=False)
    with _bound(writer, "run-1"):
        await er.ask(7, "run-1")
        await asyncio.sleep(0)
        (question_id,) = _question_ids(events)

        await er.cancel(7)
        _answer(question_id)
        await er.settle()

    assert _withdrawals(events) == []
    assert [m["result"] for m in er.sent] == [{"outcome": "answered", "value": "keep"}]
    assert not any("ERROR" in p for p in problems)
