from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class ITracingHooks(Protocol):
    """Tracing callbacks for single-hop JSON-RPC envelope spans.

    Covers the transport layer only: each individual send→response round-trip
    becomes a client span on the sender side and a server span on the receiver
    side, linked via ``_meta.traceparent`` in the message envelope.
    """

    def get_traceparent(self) -> str | None: ...
    def client_span(self, method: str, peer_id: str) -> AbstractContextManager[Any]: ...
    def server_span(self, method: str, traceparent: str | None) -> AbstractContextManager[Any]: ...
    def notification_sent(self, method: str) -> None: ...
    def notification_received(self, method: str, traceparent: str | None) -> None: ...
