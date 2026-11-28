"""A ``Backend`` that ships the query instead of interpreting it (``goals.md`` §4.10).

The store's owner executes; whoever holds the rule code asks. This is the client
half of that split, and it is deliberately thin -- it serializes, sends one
message, and deserializes. Everything interesting happens on the other side,
which is the point: the reading, the footprint capture (ADR-0013 D5) and the memo
walk all live where the store lives.

**No rule changes.** ADR-0016 D1/D2 made every terminal ``async`` precisely so
this could be substituted for ``InterpreterBackend`` without touching a rule
body, and it is: ``rule.violations(backend)`` does not know which one it has.

**One message per execution.** §4.10's "not chatty" claim is a property of *this*
class -- the unit of access is a whole query, not a read -- so ``messages_sent``
is public and counted. A property nothing can observe is a property nothing
protects, and the failure it guards against (a backend that ping-pongs per
literal) is invisible in the returned rows.

The transport is injected rather than imported. This module knows nothing about
JSON-RPC, about the WM, or about how an extension reaches it; it knows there is
something that takes a serialized query and returns a serialized result. That is
what keeps R20 true of the engine while the concrete transport lives in the
runner.
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.records import records_from_json, refs_to_json
from finecode_knowledge.query.serialize import query_to_json, result_from_json

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

    from finecode_knowledge.model.entity_type import EntityRef
    from finecode_knowledge.model.fact_source import Record
    from finecode_knowledge.query.query import Query, Result

__all__ = ["QueryTransport", "RemoteBackend", "RemoteQueryError"]


class RemoteQueryError(SchemaError):
    """The owner could not answer, named so a caller can tell it from an empty result.

    Distinct from "no rows": a rule whose query failed in transit and a rule whose
    query legitimately matched nothing are the same value if the failure is
    swallowed, and only one of them means the rule passed.
    """


class QueryTransport(typing.Protocol):
    """Whatever can carry one query to the store's owner and one result back."""

    async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict: ...

    async def fetch_records(self, refs: list[dict]) -> dict:
        """Everything known about these entities, in one message (``query/records.py``).

        The second and last method here, and it is here because a projection
        rendering an entity's whole field set has no query to compile -- the set
        is open by R18/R19's design. Keeping the read on this transport is what
        stops a consumer reaching for a ``FactSource`` instead, which is the R21
        hole this closes.
        """
        ...


class RemoteBackend:
    """Runs a ``Query`` wherever the store is, not here."""

    def __init__(self, transport: QueryTransport) -> None:
        self._transport = transport
        self.messages_sent = 0
        """How many query messages this backend has sent.

        Public so §4.10's granularity claim is assertable rather than asserted --
        see the module docstring."""

    async def run(
        self, query: Query, *, mode: Mode = Mode.VERIFIED, limit: int | None = None
    ) -> Result[list[tuple[object, ...]]]:
        """Send *query*, return what comes back.

        Raises:
            RemoteQueryError: the transport failed, or the owner returned
                something that is not a result.
        """
        payload = query_to_json(query)
        self.messages_sent += 1
        try:
            raw = await self._transport.run_query(payload, mode=mode.value, limit=limit)
        except Exception as error:
            raise RemoteQueryError(
                f"The knowledge store's owner did not answer this query: {error}"
            ) from error

        if not isinstance(raw, dict) or "rows" not in raw or "freshness" not in raw:
            raise RemoteQueryError(
                "The knowledge store's owner returned no result: expected rows and a "
                f"freshness verdict (R16), got {type(raw).__name__}."
            )
        return result_from_json(raw)

    async def records(self, refs: Sequence[EntityRef]) -> tuple[Record, ...]:
        """Everything known about *refs*, in **one** message however many there are.

        Counted in ``messages_sent`` alongside queries, because the number the
        criterion is about is round trips per execution, not round trips of one
        kind. A projection over forty handlers costs one of these, not forty --
        the batching is the whole reason the method takes a sequence.

        Raises:
            RemoteQueryError: the transport failed, or the owner returned
                something that is not a record payload.
        """
        if not refs:
            # No message at all. Asking for nothing is not a question, and a
            # round trip that can only answer "[]" is one the caller pays for.
            return ()
        self.messages_sent += 1
        try:
            raw = await self._transport.fetch_records(refs_to_json(refs))
        except Exception as error:
            raise RemoteQueryError(
                f"The knowledge store's owner did not answer this record read: {error}"
            ) from error

        if not isinstance(raw, dict) or "records" not in raw:
            raise RemoteQueryError(
                "The knowledge store's owner returned no records: expected a record "
                f"payload, got {type(raw).__name__}."
            )
        found = records_from_json(raw)
        if len(found) != len(refs):
            raise RemoteQueryError(
                f"The knowledge store's owner returned {len(found)} records for "
                f"{len(refs)} refs. The reply is positional, so a length mismatch "
                "would silently misattribute every field after the gap."
            )
        return found
