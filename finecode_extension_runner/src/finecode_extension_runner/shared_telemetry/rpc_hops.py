"""OTel hop observer: caller/receiver spans for WM<->ER requests.

Spans use the ``finecode.jsonrpc`` tracer with the names and attributes the
observability guide documents. Sampling, queueing and export are owned by the
provider setup; this observer only creates spans and events, counts sampling
decisions as S3/traces, and contains nothing itself — transport-side guards
(via :mod:`finecode_jsonrpc._hop_guard`) contain observer failures.

OTel imports stay function-local so merely importing this module never pays
the SDK import cost.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import threading
import time
import typing
from collections.abc import Generator
from typing import Literal, NotRequired, TypedDict

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
from loguru import logger

from finecode_extension_runner.shared_telemetry.health import HealthTracker

if typing.TYPE_CHECKING:
    from opentelemetry.trace import Span

__all__ = ["LateRecordKind", "LateRpcRecord", "OtelHopObserver"]

_MAX_ACTIVE_HOPS = 1024
_MAX_EVENTS_PER_HOP = 16
_HOP_TTL_SEC = 600.0
_MAX_LATE_ENTRIES = 1024
_LATE_TTL_SEC = 60.0
_MAX_LATE_EMISSIONS_PER_ENTRY = 8
_LATE_BUCKET_RATE_PER_SEC = 1.0
_LATE_BUCKET_BURST = 20
_MAX_FIELD_BYTES = 2048

LateRecordKind = Literal["post_closure_stage", "late_response", "unmatched_response"]


class LateRpcRecord(TypedDict):
    late_kind: LateRecordKind
    rpc_method: NotRequired[str]
    rpc_connection_id: str
    rpc_role: HopRole
    rpc_request_id: str
    rpc_request_id_type: Literal["int", "str"]
    rpc_generation: NotRequired[int]
    rpc_stage: NotRequired[HopStage]
    rpc_prior_outcome: str
    rpc_correlation: Literal["matched", "unavailable"]
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
    mono_ns: int


@dataclasses.dataclass
class _LateEntry:
    """Primitive-only record retained after a hop closes, for late evidence.

    Never holds spans, contexts, futures or tasks — only identifiers and a
    bounded emission count.
    """

    connection_id: str
    role: HopRole
    request_id: int | str
    method: str
    generation: int
    prior_outcome: str
    trace_id: str
    span_id: str
    expires_at: float
    emissions: int = 0


class _OpenHop:
    """Mutable per-hop state behind the opaque :class:`HopToken`."""

    __slots__ = (
        "deadline",
        "ended",
        "events",
        "generation",
        "handler_outcome",
        "identity",
        "prior_outcome",
        "role",
        "span",
    )

    def __init__(
        self,
        *,
        span: Span,
        role: HopRole,
        identity: HopIdentity,
        generation: int,
        deadline: float,
    ) -> None:
        self.span = span
        self.role = role
        self.identity = identity
        self.generation = generation
        self.deadline = deadline
        self.handler_outcome: HandlerOutcome | None = None
        self.prior_outcome = "unavailable"
        self.events = 0
        self.ended = False


class OtelHopObserver(IHopObserver):
    """Backend observer turning hop calls into OTel spans and stage events."""

    def __init__(self, role: Literal["wm", "er"], health: HealthTracker) -> None:
        self._role = role
        self._health = health
        self._lock = threading.Lock()
        self._generations = itertools.count(1)
        self._open: dict[int, _OpenHop] = {}
        self._earliest_deadline = float("inf")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._expiry_timer: asyncio.TimerHandle | None = None
        self._late: dict[tuple[str, HopRole, int | str, int], _LateEntry] = {}
        self._late_lock = threading.Lock()
        self._late_tokens = float(_LATE_BUCKET_BURST)
        self._late_refill_at = time.monotonic()

    def _start_span(
        self,
        tracer: typing.Any,
        name: str,
        context: typing.Any,
        kind: typing.Any,
        attributes: dict[str, str | int],
        start_time: int,
    ) -> typing.Any:
        """Create a recording span, else ``None`` (S3 when sampled out)."""
        try:
            span = tracer.start_span(
                name,
                context=context,
                kind=kind,
                attributes=attributes,
                start_time=start_time,
            )
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return None
        try:
            recording = span.is_recording()
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return None
        if not recording:
            self._health.count("traces", "sampled_out")
            return None
        return span

    def _set_attribute(self, span: typing.Any, key: str, value: str) -> None:
        try:
            span.set_attribute(key, value)
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")

    def _end_span(self, span: typing.Any, end_time_ns: int | None) -> None:
        try:
            if end_time_ns is None:
                span.end()
            else:
                span.end(end_time=end_time_ns)
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")

    def open_caller(
        self, identity: HopIdentity, peer: str, at: StageTime
    ) -> HopToken | None:
        if self._health.availability("traces") != "active":
            return None
        from opentelemetry import context as context_api
        from opentelemetry import trace as trace_api

        self._expire_overdue()
        with self._lock:
            over_cap = len(self._open) >= _MAX_ACTIVE_HOPS
        if over_cap:
            self._health.count("traces", "suppressed")
            return None
        generation = next(self._generations)
        try:
            tracer = trace_api.get_tracer("finecode.jsonrpc")
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return None
        span = self._start_span(
            tracer,
            f"jsonrpc.client/{identity.method}",
            context_api.get_current(),
            trace_api.SpanKind.CLIENT,
            {
                "rpc.system": "jsonrpc",
                "rpc.method": identity.method,
                "peer.id": peer,
                "finecode.rpc.connection_id": identity.connection_id,
                "finecode.rpc.role": identity.role,
                "finecode.rpc.request_id": str(identity.request_id),
                "finecode.rpc.request_id_type": _id_type(identity.request_id),
                "finecode.rpc.generation": generation,
            },
            at.wall_ns,
        )
        if span is None:
            return None
        return self._track(
            _OpenHop(
                span=span,
                role="caller",
                identity=identity,
                generation=generation,
                deadline=_deadline(at),
            )
        )

    def caller_traceparent(self, token: HopToken) -> str | None:
        from opentelemetry import propagate as propagate_api
        from opentelemetry import trace as trace_api

        state = token if isinstance(token, _OpenHop) else None
        if state is None or state.ended:
            return None
        carrier: dict[str, str] = {}
        try:
            propagate_api.inject(
                carrier, context=trace_api.set_span_in_context(state.span)
            )
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return None
        return carrier.get("traceparent")

    def open_receiver(
        self,
        identity: HopIdentity,
        traceparent: str | None,
        frame_read: StageTime,
    ) -> HopToken | None:
        if self._health.availability("traces") != "active":
            return None
        from opentelemetry import context as context_api
        from opentelemetry import propagate as propagate_api
        from opentelemetry import trace as trace_api

        parent = None
        causal_link = "unavailable"
        if traceparent:
            try:
                extracted = propagate_api.extract(
                    {"traceparent": traceparent}, context=context_api.Context()
                )
            except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
                self._health.count("traces", "contained_failure")
                extracted = None
            if (
                extracted is not None
                and trace_api.get_current_span(extracted).get_span_context().is_valid
            ):
                parent = extracted
                causal_link = "propagated"
        self._expire_overdue()
        with self._lock:
            over_cap = len(self._open) >= _MAX_ACTIVE_HOPS
        if over_cap:
            self._health.count("traces", "suppressed")
            return None
        generation = next(self._generations)
        try:
            tracer = trace_api.get_tracer("finecode.jsonrpc")
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return None
        span = self._start_span(
            tracer,
            f"jsonrpc.server/{identity.method}",
            parent if parent is not None else context_api.Context(),
            trace_api.SpanKind.SERVER,
            {
                "rpc.system": "jsonrpc",
                "rpc.method": identity.method,
                "finecode.rpc.connection_id": identity.connection_id,
                "finecode.rpc.role": identity.role,
                "finecode.rpc.request_id": str(identity.request_id),
                "finecode.rpc.request_id_type": _id_type(identity.request_id),
                "finecode.rpc.generation": generation,
                "finecode.rpc.causal_link": causal_link,
            },
            frame_read.wall_ns,
        )
        if span is None:
            return None
        token = self._track(
            _OpenHop(
                span=span,
                role="receiver",
                identity=identity,
                generation=generation,
                deadline=_deadline(frame_read),
            )
        )
        # The span starts at the frame-read wall time; record the matching
        # stage event so same-process gaps stay measurable on one clock.
        self.stage(token, "frame_read", frame_read)
        return token

    def stage(
        self,
        token: HopToken,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None:
        state = token if isinstance(token, _OpenHop) else None
        if state is None:
            self._on_post_close_stage(token, stage, at, status)
            return
        self._expire_overdue()
        with self._lock:
            if state.ended:
                ended = True
                capped = False
            elif state.events >= _MAX_EVENTS_PER_HOP:
                ended = False
                capped = True
            else:
                state.events += 1
                ended = False
                capped = False
        if ended:
            self._on_post_close_stage(token, stage, at, status)
            return
        if capped:
            self._health.count("traces", "suppressed")
            return
        attributes: dict[str, str | int] = {"finecode.stage.status": status}
        timestamp: int | None = None
        if at is not None:
            timestamp = at.wall_ns
            attributes["finecode.mono_ns"] = at.mono_ns
        try:
            state.span.add_event(
                f"rpc.stage.{stage}", attributes=attributes, timestamp=timestamp
            )
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")

    def handler_scope(self, token: HopToken) -> contextlib.AbstractContextManager[None]:
        from opentelemetry import context as context_api
        from opentelemetry import trace as trace_api

        state = token if isinstance(token, _OpenHop) else None
        if state is None or state.ended:
            return contextlib.nullcontext()
        span = state.span
        health = self._health

        @contextlib.contextmanager
        def _scope() -> Generator[None, None, None]:
            try:
                context_token = context_api.attach(trace_api.set_span_in_context(span))
            except Exception:  # noqa: BLE001 - attach contained as S10/traces
                health.count("traces", "contained_failure")
                yield
                return
            try:
                yield
            finally:
                try:
                    context_api.detach(context_token)
                except Exception:  # noqa: BLE001 - detach contained as S10/traces
                    health.count("traces", "contained_failure")

        return _scope()

    def handler_outcome(self, token: HopToken, outcome: HandlerOutcome) -> None:
        state = token if isinstance(token, _OpenHop) else None
        if state is None or state.ended:
            return
        state.handler_outcome = outcome
        self._set_attribute(state.span, "finecode.rpc.handler_outcome", outcome)

    def close_caller(
        self, token: HopToken, outcome: CallerOutcome, at: StageTime
    ) -> None:
        # The transport writer always reports via close_receiver; a close for
        # the other role is a silent no-op, so a caller span ends only here.
        state = token if isinstance(token, _OpenHop) else None
        if state is None or state.ended or state.role != "caller":
            return
        state.prior_outcome = outcome
        self._set_attribute(state.span, "finecode.rpc.caller_outcome", outcome)
        _ = self._close(token, at.wall_ns)

    def close_receiver(
        self, token: HopToken, reason: HopCloseReason, at: StageTime
    ) -> None:
        state = token if isinstance(token, _OpenHop) else None
        if state is None or state.ended or state.role != "receiver":
            return
        state.prior_outcome = reason
        self._set_attribute(state.span, "finecode.rpc.close_reason", reason)
        _ = self._close(token, at.wall_ns)

    def response_without_waiter(self, identity: HopIdentity, at: StageTime) -> None:
        if not self._admit_late():
            return
        entry = self._find_late(
            identity.connection_id, identity.role, identity.request_id
        )
        if entry is None:
            self._emit_unmatched(identity, at)
            return
        if self._overlong_identity(identity):
            self._health.count("traces", "suppressed")
            self._emit_unmatched(identity, at)
            return
        if not self._take_emission(entry):
            return
        self._emit_late(
            "late_response",
            entry,
            identity.method,
            rpc_stage=None,
            at=at,
        )

    def connection_closed(self, connection_id: str) -> None:
        with self._lock:
            remaining = [
                state
                for state in self._open.values()
                if state.identity.connection_id == connection_id
            ]
            for state in remaining:
                state.ended = True
                _ = self._open.pop(id(state), None)
            self._earliest_deadline = min(
                (state.deadline for state in self._open.values()),
                default=float("inf"),
            )
            self._rearm_timer_locked()
        for state in remaining:
            state.prior_outcome = "teardown"
            self._set_attribute(state.span, "finecode.rpc.close_reason", "teardown")
            self._end_span(state.span, None)
            self._note_closed(state)

    def report_contained_failure(self) -> None:
        self._health.count("traces", "contained_failure")

    def _track(self, state: _OpenHop) -> HopToken:
        with self._lock:
            self._open[id(state)] = state
            self._earliest_deadline = min(self._earliest_deadline, state.deadline)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return state
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = loop
                self._cancel_timer_locked()
            if loop is not self._loop:
                return state
            self._rearm_timer_locked()
        return state

    def _expire_overdue(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now < self._earliest_deadline:
                return
            due = [state for state in self._open.values() if state.deadline <= now]
            for state in due:
                state.ended = True
                _ = self._open.pop(id(state), None)
            self._earliest_deadline = min(
                (state.deadline for state in self._open.values()),
                default=float("inf"),
            )
            self._rearm_timer_locked()
        for state in due:
            state.prior_outcome = "expired"
            self._set_attribute(state.span, "finecode.rpc.close_reason", "expired")
            self._end_span(state.span, None)
            self._note_closed(state)

    def _cancel_timer_locked(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None

    def _rearm_timer_locked(self) -> None:
        self._cancel_timer_locked()
        if not self._open or self._loop is None:
            return
        delay = max(0.0, self._earliest_deadline - self._loop.time())
        self._expiry_timer = self._loop.call_later(delay, self._expire_due)

    def _expire_due(self) -> None:
        self._expire_overdue()

    def _note_closed(self, state: _OpenHop) -> None:
        """Retain a primitive-only late entry for a closed hop."""
        try:
            span_context = state.span.get_span_context()
            trace_id = f"{span_context.trace_id:032x}"
            span_id = f"{span_context.span_id:016x}"
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            return
        try:
            now = time.monotonic()
        except Exception:  # noqa: BLE001 - a clock failure sets the sticky flag
            self._health.set_flag("clock_failure")
            return
        entry = _LateEntry(
            connection_id=state.identity.connection_id,
            role=state.identity.role,
            request_id=state.identity.request_id,
            method=state.identity.method,
            generation=state.generation,
            prior_outcome=state.prior_outcome,
            trace_id=trace_id,
            span_id=span_id,
            expires_at=now + _LATE_TTL_SEC,
        )
        with self._late_lock:
            self._purge_late_locked(now)
            if len(self._late) >= _MAX_LATE_ENTRIES:
                over_cap = True
            else:
                over_cap = False
                self._late[_late_key(entry)] = entry
        if over_cap:
            self._health.count("traces", "suppressed")

    def _close(self, token: HopToken, end_time_ns: int) -> bool:
        state = token if isinstance(token, _OpenHop) else None
        if state is None:
            return False
        with self._lock:
            if state.ended:
                return False
            state.ended = True
            _ = self._open.pop(id(state), None)
            self._earliest_deadline = min(
                (open_state.deadline for open_state in self._open.values()),
                default=float("inf"),
            )
            self._rearm_timer_locked()
        self._end_span(state.span, end_time_ns)
        self._note_closed(state)
        return True

    def _on_post_close_stage(
        self,
        token: HopToken,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus,
    ) -> None:
        """Log a stage observed after its hop closed, when retained."""
        state = token if isinstance(token, _OpenHop) else None
        if state is None:
            return
        if not self._admit_late():
            return
        entry = self._find_late_entry(state)
        if entry is None:
            self._emit_unmatched(state.identity, at)
            return
        if self._overlong_identity(state.identity):
            self._health.count("traces", "suppressed")
            self._emit_unmatched(state.identity, at)
            return
        if not self._take_emission(entry):
            return
        self._emit_late(
            "post_closure_stage",
            entry,
            state.identity.method,
            rpc_stage=stage,
            at=at,
            detail=f"stage={stage} status={status}",
        )

    def _admit_late(self) -> bool:
        """Admit one late record through the process token bucket.

        The non-blocking lock guards only the clock read and token update;
        contention sets the sticky contention flag, a clock failure sets the
        clock-failure flag, and any rejection counts S7/traces.
        """
        if not self._late_lock.acquire(blocking=False):
            self._health.set_flag("contention")
            self._health.count("traces", "suppressed")
            return False
        try:
            try:
                now = time.monotonic()
            except Exception:  # noqa: BLE001 - clock failure is sticky, never fatal
                self._health.set_flag("clock_failure")
                self._health.count("traces", "suppressed")
                return False
            elapsed = now - self._late_refill_at
            self._late_tokens = min(
                float(_LATE_BUCKET_BURST),
                self._late_tokens + elapsed * _LATE_BUCKET_RATE_PER_SEC,
            )
            self._late_refill_at = now
            if self._late_tokens < 1.0:
                admitted = False
            else:
                self._late_tokens -= 1.0
                admitted = True
        finally:
            self._late_lock.release()
        if not admitted:
            self._health.count("traces", "suppressed")
            return False
        return True

    def _purge_late_locked(self, now: float) -> None:
        expired = [key for key, entry in self._late.items() if entry.expires_at <= now]
        for key in expired:
            _ = self._late.pop(key, None)

    def _find_late_entry(self, state: _OpenHop) -> _LateEntry | None:
        with self._late_lock:
            self._purge_late_locked(time.monotonic())
            return self._late.get(_late_key_for(state))

    def _find_late(
        self, connection_id: str, role: HopRole, request_id: int | str
    ) -> _LateEntry | None:
        """Most recent unexpired entry for a namespace, any generation."""
        now = time.monotonic()
        found: _LateEntry | None = None
        with self._late_lock:
            self._purge_late_locked(now)
            for entry in self._late.values():
                if (
                    entry.connection_id == connection_id
                    and entry.role == role
                    and entry.request_id == request_id
                    and type(entry.request_id) is type(request_id)
                ):
                    found = entry
        return found

    def _take_emission(self, entry: _LateEntry) -> bool:
        with self._late_lock:
            if entry.emissions >= _MAX_LATE_EMISSIONS_PER_ENTRY:
                capped = True
            else:
                entry.emissions += 1
                capped = False
        if capped:
            self._health.count("traces", "suppressed")
            return False
        return True

    @staticmethod
    def _overlong_identity(identity: HopIdentity) -> bool:
        for field in (
            identity.connection_id,
            identity.method,
            str(identity.request_id),
        ):
            if len(field.encode("utf-8")) > _MAX_FIELD_BYTES:
                return True
        return False

    def _emit_late(
        self,
        kind: Literal["post_closure_stage", "late_response"],
        entry: _LateEntry,
        method: str,
        rpc_stage: HopStage | None,
        at: StageTime | None,
        detail: str = "",
    ) -> None:
        from opentelemetry import context as context_api
        from opentelemetry import trace as trace_api

        try:
            mono_ns = at.mono_ns if at is not None else time.monotonic_ns()
        except Exception:  # noqa: BLE001 - a clock failure sets the sticky flag
            self._health.set_flag("clock_failure")
            return
        record: LateRpcRecord = {
            "late_kind": kind,
            "rpc_connection_id": entry.connection_id,
            "rpc_role": entry.role,
            "rpc_request_id": str(entry.request_id),
            "rpc_request_id_type": _id_type(entry.request_id),
            "rpc_generation": entry.generation,
            "rpc_prior_outcome": entry.prior_outcome,
            "rpc_correlation": "matched",
            "trace_id": entry.trace_id,
            "span_id": entry.span_id,
            "mono_ns": mono_ns,
        }
        if method:
            record["rpc_method"] = method
        if rpc_stage is not None:
            record["rpc_stage"] = rpc_stage
        restored: context_api.Context | None = None
        try:
            restored = trace_api.set_span_in_context(
                trace_api.NonRecordingSpan(
                    trace_api.SpanContext(
                        trace_id=int(entry.trace_id, 16),
                        span_id=int(entry.span_id, 16),
                        is_remote=True,
                    )
                )
            )
            token = context_api.attach(restored)
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            token = None
        try:
            logger.bind(**record).info(
                f"late RPC telemetry record ({kind}): "
                f"method={method or '?'} "
                f"connection={entry.connection_id} "
                f"request={entry.request_id} prior={entry.prior_outcome}"
                + (f" {detail}" if detail else "")
            )
        finally:
            if token is not None:
                try:
                    context_api.detach(token)
                except Exception:  # noqa: BLE001 - contained once as S10/traces
                    self._health.count("traces", "contained_failure")

    def _emit_unmatched(self, identity: HopIdentity, at: StageTime | None) -> None:
        from opentelemetry import context as context_api

        try:
            mono_ns = at.mono_ns if at is not None else time.monotonic_ns()
        except Exception:  # noqa: BLE001 - a clock failure sets the sticky flag
            self._health.set_flag("clock_failure")
            return
        record: LateRpcRecord = {
            "late_kind": "unmatched_response",
            "rpc_connection_id": identity.connection_id,
            "rpc_role": identity.role,
            "rpc_request_id": str(identity.request_id),
            "rpc_request_id_type": _id_type(identity.request_id),
            "rpc_prior_outcome": "unavailable",
            "rpc_correlation": "unavailable",
            "mono_ns": mono_ns,
        }
        if identity.method:
            record["rpc_method"] = identity.method
        try:
            token = context_api.attach(context_api.Context())
        except Exception:  # noqa: BLE001 - contained and counted once as S10/traces
            self._health.count("traces", "contained_failure")
            token = None
        try:
            logger.bind(**record).info(
                f"late RPC telemetry record (unmatched_response): "
                f"method={identity.method or '?'} "
                f"connection={identity.connection_id} "
                f"request={identity.request_id}"
            )
        finally:
            if token is not None:
                try:
                    context_api.detach(token)
                except Exception:  # noqa: BLE001 - contained once as S10/traces
                    self._health.count("traces", "contained_failure")


def _id_type(request_id: int | str) -> Literal["int", "str"]:
    return "int" if isinstance(request_id, int) else "str"


def _late_key(entry: _LateEntry) -> tuple[str, HopRole, int | str, int]:
    return (
        entry.connection_id,
        entry.role,
        entry.request_id,
        entry.generation,
    )


def _late_key_for(state: _OpenHop) -> tuple[str, HopRole, int | str, int]:
    return (
        state.identity.connection_id,
        state.identity.role,
        state.identity.request_id,
        state.generation,
    )


def _deadline(at: StageTime) -> float:
    """Monotonic-seconds expiry deadline for a hop opened at *at*."""
    return at.mono_ns / 1_000_000_000 + _HOP_TTL_SEC
