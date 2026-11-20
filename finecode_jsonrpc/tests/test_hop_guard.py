"""Guards around hop-observer calls contain observer failures.

If the telemetry observer raises, RPC outcomes must be unchanged — the error
is counted once via ``report_contained_failure`` and never propagates into
transport work. Cancellation must always propagate.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType
from typing import cast

from finecode_jsonrpc._hop_guard import (
    call_guarded,
    guarded_handler_scope,
    report_failure_guarded,
)
from finecode_jsonrpc.tracing import (
    CallerOutcome,
    HandlerOutcome,
    HopCloseReason,
    HopIdentity,
    HopRole,
    HopStage,
    HopToken,
    IHopObserver,
    StageStatus,
    StageTime,
)


def _token() -> HopToken:
    return cast(HopToken, object())


class _FakeObserver:
    """IHopObserver double that raises on one named hook, counts reports."""

    def __init__(
        self, raise_on: str | None = None, report_raises: bool = False
    ) -> None:
        self.raise_on = raise_on
        self.reports = 0
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._report_raises = report_raises

    def _maybe_raise(self, name: str) -> None:
        if self.raise_on == name:
            raise RuntimeError(f"{name} failed")

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def report_contained_failure(self) -> None:
        self.reports += 1
        if self._report_raises:
            raise RuntimeError("report failed")

    def open_caller(
        self, identity: HopIdentity, peer: str, at: StageTime
    ) -> HopToken | None:
        self._record("open_caller", identity, peer, at)
        self._maybe_raise("open_caller")
        return _token()

    def caller_traceparent(self, token: HopToken) -> str | None:
        self._record("caller_traceparent", token)
        self._maybe_raise("caller_traceparent")
        return None

    def open_receiver(
        self,
        identity: HopIdentity,
        traceparent: str | None,
        frame_read: StageTime,
    ) -> HopToken | None:
        self._record("open_receiver", identity, traceparent, frame_read)
        self._maybe_raise("open_receiver")
        return _token()

    def stage(
        self,
        token: HopToken,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        self._record("stage", token, stage, at, status)
        self._maybe_raise("stage")

    def handler_scope(self, token: HopToken) -> contextlib.AbstractContextManager[None]:
        self._record("handler_scope", token)
        self._maybe_raise("handler_scope")
        return contextlib.nullcontext()

    def handler_outcome(self, token: HopToken, outcome: HandlerOutcome) -> None:
        self._record("handler_outcome", token, outcome)
        self._maybe_raise("handler_outcome")

    def close_caller(
        self, token: HopToken, outcome: CallerOutcome, at: StageTime
    ) -> None:
        self._record("close_caller", token, outcome, at)
        self._maybe_raise("close_caller")

    def close_receiver(
        self, token: HopToken, reason: HopCloseReason, at: StageTime
    ) -> None:
        self._record("close_receiver", token, reason, at)
        self._maybe_raise("close_receiver")

    def response_without_waiter(self, identity: HopIdentity, at: StageTime) -> None:
        self._record("response_without_waiter", identity, at)
        self._maybe_raise("response_without_waiter")

    def connection_closed(self, connection_id: str) -> None:
        self._record("connection_closed", connection_id)
        self._maybe_raise("connection_closed")


def _as_observer(fake: _FakeObserver) -> IHopObserver:
    return cast(IHopObserver, fake)


def test_call_guarded_contains_exception_and_reports_once() -> None:
    """An observer raising leaves the guarded call contained and counted."""

    observer = _FakeObserver(raise_on="stage")

    def _boom() -> None:
        observer.stage(_token(), "dispatched", None)

    assert call_guarded(_as_observer(observer), _boom) is None
    assert observer.reports == 1


def test_call_guarded_survives_raising_report() -> None:
    """A failing report path never propagates into transport work."""

    observer = _FakeObserver(raise_on="stage", report_raises=True)

    def _boom() -> None:
        observer.stage(_token(), "dispatched", None)

    assert call_guarded(_as_observer(observer), _boom) is None
    assert observer.reports == 1


def test_call_guarded_lets_cancellation_through() -> None:
    """Cancellation from the observer is never contained."""

    observer = _FakeObserver()

    def _cancel() -> None:
        raise asyncio.CancelledError()

    try:
        call_guarded(_as_observer(observer), _cancel)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must propagate")
    assert observer.reports == 0


def test_report_guarded_lets_cancellation_through() -> None:
    """Cancellation from the report path itself propagates."""

    class _CancellingReport(_FakeObserver):
        def report_contained_failure(self) -> None:
            raise asyncio.CancelledError()

    observer = _CancellingReport()
    try:
        report_failure_guarded(_as_observer(observer))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must propagate")


def test_guarded_scope_contains_enter_and_ignores_truthy_exit() -> None:
    """A failed enter is a no-op; a truthy exit never suppresses the body."""

    observer = _FakeObserver(raise_on="handler_scope")
    with guarded_handler_scope(_as_observer(observer), _token()):
        pass
    assert observer.reports == 1

    class _TruthyExit:
        def __init__(self) -> None:
            self.seen: list[tuple[object, object, object]] = []

        def __enter__(self) -> None:
            return None

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool:
            self.seen.append((exc_type, exc, tb))
            return True

    class _TruthyObserver(_FakeObserver):
        def handler_scope(
            self, token: HopToken
        ) -> contextlib.AbstractContextManager[None]:
            self._record("handler_scope", token)
            return cast(contextlib.AbstractContextManager[None], _TruthyExit())

    truthy = _TruthyObserver()
    try:
        with guarded_handler_scope(_as_observer(truthy), _token()):
            raise ValueError("body must survive truthy exit")
    except ValueError:
        pass
    else:
        raise AssertionError("body exception must propagate")
    assert truthy.reports == 0


def test_guarded_scope_contains_exit_failure() -> None:
    """An exit raising is contained and counted."""

    class _BadExit:
        def __init__(self) -> None:
            self.seen: list[tuple[object, object, object]] = []

        def __enter__(self) -> None:
            return None

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            self.seen.append((exc_type, exc, tb))
            raise RuntimeError("exit failed")

    class _ExitFailingObserver(_FakeObserver):
        def handler_scope(
            self, token: HopToken
        ) -> contextlib.AbstractContextManager[None]:
            self._record("handler_scope", token)
            return cast(contextlib.AbstractContextManager[None], _BadExit())

    observer = _ExitFailingObserver()
    with guarded_handler_scope(_as_observer(observer), _token()):
        pass
    assert observer.reports == 1


def test_hop_identity_preserves_wire_id_type() -> None:
    """Identity keeps the wire request-id type for later typed attribution."""

    int_identity = HopIdentity(
        connection_id="c1", role="caller", request_id=7, method="m"
    )
    str_identity = HopIdentity(
        connection_id="c1", role="caller", request_id="7", method="m"
    )
    assert isinstance(int_identity.request_id, int)
    assert isinstance(str_identity.request_id, str)
    role: HopRole = int_identity.role
    assert role == "caller"
