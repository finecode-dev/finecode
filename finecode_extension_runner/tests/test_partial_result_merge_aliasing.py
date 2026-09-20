"""Partial-result streaming must not double-merge or leak mutation.

The run's accumulator and the client-side coalescer each retain the first
result object a handler sends, so sharing that object between them used to
merge every later send into one object twice — corrupting every append-merging
result type — and left both accumulators aliasing a handler-owned object a
handler could mutate after sending. These tests pin the copies on both sides,
the coverage and exception-path flush behaviour, and the harness surface that
makes streaming observable.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from pathlib import Path

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri
from loguru import logger

from finecode_extension_runner import coverage_sink
from finecode_extension_runner import partial_result_sender as prs_module
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import Session, handler_test_session

_MISS_URI = ResourceUri("file:///input.py")

# The import-time sender, which raises by construction. Rebinding the module
# global in a test must be undone, or the next test that needs the unwired
# default inherits a bound sender.
_DEFAULT_SENDER = run_action_service.partial_result_sender

_sent_originals: list[code_action.RunActionResult] = []


@dataclasses.dataclass
class _AppendResult(code_action.RunActionResult):
    """Append-merging result, shaped like fine_test.RunTestsRunResult: every
    ``update()`` concatenates rather than replacing."""

    text: str = ""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _AppendResult):
            self.text += other.text


@dataclasses.dataclass
class _DiagRecord:
    message: str
    code: int


@dataclasses.dataclass
class _DiagResult(code_action.RunActionResult):
    """Result with a populated dict field, shaped like fine_lint.LintRunResult."""

    diagnostics: dict[ResourceUri, list[_DiagRecord]]

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _DiagResult):
            for key, values in other.diagnostics.items():
                self.diagnostics.setdefault(key, []).extend(values)


@dataclasses.dataclass
class _AnyResult(code_action.RunActionResult):
    """Result with an open-typed field, shaped like RunAgentTaskRunResult."""

    payload: typing.Any = None

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _AnyResult):
            self.payload = other.payload


class _SendContext(code_action.RunActionContext[code_action.RunActionPayload]): ...


class _SendAction(
    code_action.Action[code_action.RunActionPayload, _SendContext, _AppendResult]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = _SendContext
    RESULT_TYPE = _AppendResult
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL


_ACTION_NAME = _SendAction.__name__
_ACTION_SOURCE = f"{_SendAction.__module__}.{_SendAction.__qualname__}"


def _actions_with(handler_cls: type) -> dict:
    return {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": [
                {
                    "name": "send",
                    "source": f"{handler_cls.__module__}.{handler_cls.__qualname__}",
                }
            ],
        }
    }


class _StreamOnceHandler(
    code_action.ActionHandler[_SendAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _SendContext,
    ) -> None:
        await run_context.partial_result_sender.send(_AppendResult(text="a"))


class _StreamOnceAndAbsorbHandler(
    code_action.ActionHandler[_SendAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _SendContext,
    ) -> None:
        coverage_sink.absorb_coverage([_MISS_URI])
        await run_context.partial_result_sender.send(_AppendResult(text="a"))


class _StreamThenRaiseHandler(
    code_action.ActionHandler[_SendAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _SendContext,
    ) -> None:
        await run_context.partial_result_sender.send(_AppendResult(text="a"))
        raise RuntimeError("handler failed")


class _StreamThenRaiseValueErrorHandler(
    code_action.ActionHandler[_SendAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _SendContext,
    ) -> None:
        await run_context.partial_result_sender.send(_AppendResult(text="a"))
        raise ValueError("handler failed")


class _StreamTwiceRecordsOriginalsHandler(
    code_action.ActionHandler[_SendAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _SendContext,
    ) -> None:
        first = _AppendResult(text="a")
        second = _AppendResult(text="b")
        _sent_originals[:] = [first, second]
        await run_context.partial_result_sender.send(first)
        await run_context.partial_result_sender.send(second)


async def test_ac1_append_merging_result_is_not_double_merged() -> None:
    """Three append-merging partials inside one debounce window must each be
    counted exactly once in both the run's accumulated result and the single
    coalesced value delivered to the client. A double count corrupts result
    text that callers aggregate downstream (test runs, code-action lists,
    env-install errors)."""
    delivered: list[code_action.RunActionResult] = []

    def _fake_send(token, value, formats=None) -> None:
        delivered.append(value)

    coalescer = prs_module.PartialResultSender(sender=_fake_send, wait_time_ms=300)
    accumulator = run_action_service._PartialResultAccumulator(
        token="tok-1", send_func=coalescer.schedule_sending
    )
    for text in ("a", "b", "c"):
        await accumulator.send(_AppendResult(text=text))
    await coalescer.send_all_immediately()

    assert accumulator.accumulated is not None
    assert accumulator.accumulated.text == "abc"
    assert len(delivered) == 1
    assert delivered[0].text == "abc"


async def test_ac2_mutating_a_sent_result_after_sending_has_no_effect() -> None:
    """A handler that mutates a result object after passing it to send() must
    leave both the accumulated result and the delivered value untouched. If
    either accumulator still shared the handler's object, a mutating handler
    could corrupt the client's stream and the action result differently."""
    delivered: list[code_action.RunActionResult] = []

    def _fake_send(token, value, formats=None) -> None:
        delivered.append(value)

    coalescer = prs_module.PartialResultSender(sender=_fake_send, wait_time_ms=300)
    accumulator = run_action_service._PartialResultAccumulator(
        token="tok-1", send_func=coalescer.schedule_sending
    )
    first = _AppendResult(text="a")
    await accumulator.send(first)
    first.text = "MUTATED"
    await accumulator.send(_AppendResult(text="b"))
    await coalescer.send_all_immediately()

    assert accumulator.accumulated is not None
    assert accumulator.accumulated.text == "ab"
    assert len(delivered) == 1
    assert delivered[0].text == "ab"


async def test_ac3_copy_round_trip_preserves_result_shapes() -> None:
    """The internal copy of a sent result must round-trip result types
    exactly — including dict fields keyed by URI, open-typed fields, and the
    coverage list carried by every result type. A lossy round-trip would
    corrupt any streamed result type the moment it is sent."""
    cases: list[code_action.RunActionResult] = [
        _DiagResult(
            diagnostics={
                ResourceUri("file:///a.py"): [_DiagRecord(message="x", code=1)],
                ResourceUri("file:///b.py"): [
                    _DiagRecord(message="y", code=2),
                    _DiagRecord(message="z", code=3),
                ],
            }
        ),
        _AnyResult(payload={"nested": [1, {"two": 2}], "three": None}),
        _AppendResult(
            text="a",
            coverage=[
                ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
            ],
        ),
    ]
    delivered: list[code_action.RunActionResult] = []
    for original in cases:
        accumulator = run_action_service._PartialResultAccumulator(token="tok-1")
        await accumulator.send(original)
        assert accumulator.accumulated == original

        delivered.clear()
        coalescer = prs_module.PartialResultSender(
            sender=lambda token, value, formats=None: delivered.append(value),
            wait_time_ms=300,
        )
        await coalescer.schedule_sending("tok-1", original)
        await coalescer.send_all_immediately()
        assert delivered == [original]


async def test_ac4_absorbed_coverage_survives_a_streamed_partial(
    tmp_path: Path,
) -> None:
    """A streamed partial sent by a handler that also absorbed coverage
    mid-run must still leave that coverage on the final action result.
    Coverage is how degraded results are reported; losing it on the streaming
    path would hide absorbed misses from every consumer."""

    def _fake_send(token, value, formats=None) -> None:
        pass

    run_action_service.set_partial_result_sender(_fake_send)
    try:
        async with handler_test_session(
            project_dir=tmp_path, actions=_actions_with(_StreamOnceAndAbsorbHandler)
        ) as session:
            result = await session.run_action(
                _ACTION_NAME, partial_result_token="tok-1"
            )
    finally:
        run_action_service.partial_result_sender = _DEFAULT_SENDER

    assert result is not None
    assert result.coverage == [
        ItemCoverage(status=CoverageStatus.ABSORBED, item=_MISS_URI)
    ]


async def test_ac5_partial_sent_before_raising_is_still_delivered(
    tmp_path: Path,
) -> None:
    """A partial sent immediately before a handler raises must still reach the
    client exactly once. Without the exit-path flush it waits out the debounce
    and races the error response, so a client sees a stream cut off exactly
    when reporting the failure."""
    delivered: list[code_action.RunActionResult] = []

    def _fake_send(token, value, formats=None) -> None:
        delivered.append(value)

    run_action_service.set_partial_result_sender(_fake_send)
    try:
        async with handler_test_session(
            project_dir=tmp_path, actions=_actions_with(_StreamThenRaiseHandler)
        ) as session:
            with pytest.raises(run_action_service.ActionFailedException):
                await session.run_action(_ACTION_NAME, partial_result_token="tok-1")
    finally:
        run_action_service.partial_result_sender = _DEFAULT_SENDER

    assert len(delivered) == 1
    assert delivered[0].text == "a"


async def test_ac6_flush_failure_never_replaces_the_unwinding_exception(
    tmp_path: Path,
) -> None:
    """A flush failure while an exception is already unwinding must not replace
    that exception. The unwired default sender raises by construction, so an
    unguarded flush on the exit path would turn every direct-send handler
    error into a different, misleading one."""
    run_action_service.set_partial_result_sender(
        run_action_service._unwired_partial_result_send
    )
    records: list[str] = []
    sink_id = logger.add(
        lambda message: records.append(message.record["message"]), level="ERROR"
    )
    try:
        async with handler_test_session(
            project_dir=tmp_path,
            actions=_actions_with(_StreamThenRaiseValueErrorHandler),
        ) as session:
            with pytest.raises(run_action_service.ActionFailedException) as exc_info:
                await session.run_action(_ACTION_NAME, partial_result_token="tok-1")
    finally:
        logger.remove(sink_id)
        run_action_service.partial_result_sender = _DEFAULT_SENDER

    assert "handler failed" in exc_info.value.message
    assert any(
        "Flushing partial results on exit failed" in record for record in records
    )


async def test_ac7_observer_receives_the_exact_objects_the_handler_sent(
    tmp_path: Path,
) -> None:
    """The test session's partial-result observer must hold the exact objects a
    handler passed to send() — identity, not copies — in order. That is the
    only way a harness can assert that a handler streamed specific values."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_actions_with(_StreamTwiceRecordsOriginalsHandler)
    ) as session:
        result = await session.run_action(_ACTION_NAME)

    assert result is not None
    assert result.text == "ab"
    assert len(session.partial_results.events) == 2
    assert session.partial_results.events[0] is _sent_originals[0]
    assert session.partial_results.events[1] is _sent_originals[1]


async def test_ac8_session_token_reaches_the_streaming_path_and_default_stays_none(
    tmp_path: Path,
) -> None:
    """``Session.run_action`` must pass a supplied token through to the
    streaming path, and keep no-token runs on the non-streaming path by
    default — without the pass-through, no shipped harness could exercise
    streaming end to end."""
    delivered: list[code_action.RunActionResult] = []

    def _fake_send(token, value, formats=None) -> None:
        delivered.append(value)

    run_action_service.set_partial_result_sender(_fake_send)
    try:
        async with handler_test_session(
            project_dir=tmp_path, actions=_actions_with(_StreamOnceHandler)
        ) as session:
            result = await session.run_action(_ACTION_NAME)
        assert result is not None and result.text == "a"
        assert delivered == []  # no token: nothing is wired to the client

        async with handler_test_session(
            project_dir=tmp_path, actions=_actions_with(_StreamOnceHandler)
        ) as session:
            result = await session.run_action(
                _ACTION_NAME, partial_result_token="tok-1"
            )
        assert result is not None and result.text == "a"
        assert len(delivered) == 1
    finally:
        run_action_service.partial_result_sender = _DEFAULT_SENDER

    parameters = inspect.signature(Session.run_action).parameters
    assert parameters["partial_result_token"].default is None
