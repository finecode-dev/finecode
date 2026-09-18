"""The backend boundary (ADR-0016 D2).

One method, taking a **whole** ``Query``. ADR-0013 D1 already fixed the
granularity: a compiler takes a query, not a stream of reads, and ``goals.md``
§4.10 says one query in, one result out. Driving an engine per-literal would
cost a round-trip per literal per binding *and* forgo the planner that is the
only reason to adopt one.

Asynchrony attaches **here**, at the boundary where the network actually is --
not at ``FactSource``, which stays synchronous (ADR-0013 D2). The interpreter's
``run`` is an ``async def`` that never awaits, which is not a contradiction of
that argument but an application of it: D2 rejected async on a protocol called
once per literal per binding; this is one coroutine per query *execution*.
"""

from __future__ import annotations

import enum
import typing

if typing.TYPE_CHECKING:
    from finecode_knowledge.query.query import Query, Result

__all__ = ["Backend", "Mode"]


class Mode(enum.Enum):
    """Per-terminal read mode -- ``goals.md`` Q1's per-query granularity."""

    VERIFIED = "verified"
    """Default: blocks until inputs verify. A verified-mode result may carry
    ``UNTRACKED``, ``CONTESTED`` and ``STALE`` reservations -- only ``CACHED`` is
    unreachable by construction, which is what makes the mode's contract testable.

    ``STALE`` was added to that list by ADR-0022 D2, and deliberately: verified
    mode is where a fact file whose sources have moved matters *most*, so "the
    sources moved" is exactly the thing this mode must be able to report rather
    than the thing its contract excludes."""
    CACHED = "cached"
    """Opt-in: memoized value, tagged with a ``CACHED`` reservation when it was
    not verified at the pinned revision."""


class Backend(typing.Protocol):
    async def run(
        self, query: Query, *, mode: Mode, limit: int | None = None
    ) -> Result[tuple[object, ...]]: ...
