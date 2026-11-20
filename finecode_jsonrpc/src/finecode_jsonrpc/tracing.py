from __future__ import annotations

import contextlib
import dataclasses
import time
from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol

__all__ = [
    "CallerOutcome",
    "HandlerOutcome",
    "HopCloseReason",
    "HopIdentity",
    "HopRole",
    "HopStage",
    "HopToken",
    "IHopObserver",
    "StageStatus",
    "StageTime",
    "capture_stage_time",
]


class ITracingHooks(Protocol):
    """Tracing callbacks for single-hop JSON-RPC envelope spans.

    Covers the transport layer only: each individual send→response round-trip
    becomes a client span on the sender side and a server span on the receiver
    side, linked via ``_meta.traceparent`` in the message envelope.
    """

    def get_traceparent(self) -> str | None: ...
    def client_span(self, method: str, peer_id: str) -> AbstractContextManager[Any]: ...
    def server_span(
        self, method: str, traceparent: str | None
    ) -> AbstractContextManager[Any]: ...
    def notification_sent(self, method: str) -> None: ...
    def notification_received(self, method: str, traceparent: str | None) -> None: ...


HopRole = Literal["caller", "receiver"]
HopStage = Literal[
    "submitted",
    "enqueued",
    "dequeued",
    "write_returned",
    "drain_started",
    "drain_returned",
    "frame_read",
    "dispatched",
    "handler_started",
    "handler_ended",
    "response_resolved",
    "caller_completed",
]
StageStatus = Literal["observed", "unsupported", "failed"]
CallerOutcome = Literal[
    "result",
    "remote_error",
    "invalid_response",
    "timeout",
    "cancelled",
    "disconnected",
    "send_failed",
]
HandlerOutcome = Literal["returned", "raised", "cancelled", "not_executed"]
HopCloseReason = Literal["completed", "write_failed", "teardown", "expired"]


@dataclasses.dataclass(frozen=True, slots=True)
class StageTime:
    mono_ns: int  # time.monotonic_ns(), this process's one elapsed clock
    wall_ns: int  # time.time_ns(), presentation only


def capture_stage_time() -> StageTime:
    """Capture the current stage time (monotonic + wall clocks)."""
    return StageTime(mono_ns=time.monotonic_ns(), wall_ns=time.time_ns())


@dataclasses.dataclass(frozen=True, slots=True)
class HopIdentity:
    connection_id: str  # fresh per connection incarnation, minted by the endpoint
    role: HopRole  # request-origin direction relative to this endpoint
    request_id: int | str  # wire ID, wire type preserved
    method: str


class HopToken(Protocol): ...  # opaque to transports


class IHopObserver(Protocol):
    """Backend-neutral observer of JSON-RPC request hops.

    Contract:

    - ``open_*`` returning ``None`` means "not observed" (telemetry off,
      sampled out, cap reached); the transport then performs no further hop
      calls or clock reads for that request beyond the one already taken.
    - ``open_caller`` runs on the caller's task and reads its current
      context; ``open_receiver`` must not read ambient context.
    - ``stage``, ``close_*``, ``response_without_waiter`` and
      ``connection_closed`` may be called from the IO thread or the owning
      loop and must not block.
    - The transport writer always reports completion via ``close_receiver``;
      a ``close_*`` call naming a token opened with the other role is a
      silent no-op (the session owns the matching ``close_caller``).
    - Every call is made through a transport-side guard that contains an
      ordinary ``Exception``, ignores a truthy ``__exit__``, and then calls
      ``report_contained_failure()`` under its own guard.
    """

    def open_caller(
        self, identity: HopIdentity, peer: str, at: StageTime
    ) -> HopToken | None: ...
    def caller_traceparent(self, token: HopToken) -> str | None: ...
    def open_receiver(
        self,
        identity: HopIdentity,
        traceparent: str | None,
        frame_read: StageTime,
    ) -> HopToken | None: ...
    def stage(
        self,
        token: HopToken,
        stage: HopStage,
        at: StageTime | None,
        status: StageStatus = "observed",
    ) -> None: ...
    def handler_scope(
        self, token: HopToken
    ) -> contextlib.AbstractContextManager[None]: ...
    def handler_outcome(self, token: HopToken, outcome: HandlerOutcome) -> None: ...
    def close_caller(
        self, token: HopToken, outcome: CallerOutcome, at: StageTime
    ) -> None: ...
    def close_receiver(
        self, token: HopToken, reason: HopCloseReason, at: StageTime
    ) -> None: ...
    def response_without_waiter(self, identity: HopIdentity, at: StageTime) -> None: ...
    def connection_closed(self, connection_id: str) -> None: ...
    def report_contained_failure(self) -> None: ...
