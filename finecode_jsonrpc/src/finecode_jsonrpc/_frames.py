"""Private queue items shared by JSON-RPC transports.

These never leave ``finecode_jsonrpc``: they ride the internal producer/consumer
queues so hop tokens travel with their serialized payloads.
"""

from __future__ import annotations

import dataclasses
import typing

from .tracing import HopToken, StageTime

__all__ = ["OutgoingFrame", "ReceivedFrame"]


@dataclasses.dataclass(frozen=True, slots=True)
class OutgoingFrame:
    """One serialized message plus the hop it belongs to (if observed)."""

    data: bytes
    hop: HopToken | None


@dataclasses.dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """One parsed inbound message plus when its frame completed reading."""

    message: dict[str, typing.Any]
    received: StageTime
