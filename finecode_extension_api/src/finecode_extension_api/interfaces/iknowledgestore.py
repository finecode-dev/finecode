from __future__ import annotations

import typing

from finecode_extension_api import service

__all__ = ["IKnowledgeStore"]


class IKnowledgeStore(service.Service, typing.Protocol):
    """Read access to the workspace's knowledge store, which the WM owns.

    An extension that hosts rule code does **not** open the store. It sends the
    WM a query and gets rows plus a freshness verdict back -- one message per
    query, on demand. The WM executes against its own backend, records what the
    query read, and decides whether the answer
    had to be recomputed at all. None of that is visible here, deliberately:
    swapping the WM's storage backend changes nothing an extension sees.

    **The payloads are opaque on purpose.** ``snapshot``, ``query`` and the
    returned rows are JSON documents whose shape belongs to the knowledge engine,
    which builds and reads them. Restating that shape
    in this package would give one wire format two owners and let them drift; an
    extension normally never touches these dicts directly, because the engine
    adapts this service into an ordinary query backend.
    """

    async def register_schema(self, snapshot: dict) -> bool:
        """Hand the WM the schema its store should be read against.

        Sent once, before the first query. Returns whether the WM took a new
        schema -- ``False`` means it already held this one, which is the ordinary
        case for a second call.
        """
        ...

    async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict:
        """Execute *query* against the WM's store; return rows and a verdict.

        *mode* is the read mode. ``"verified"`` blocks until inputs verify --
        the right default for a gate, where a stale result is worse than a slow
        one. ``"cached"`` returns whatever the WM last memoized for this query
        immediately, with a ``CACHED`` reservation saying it was not
        re-verified; with nothing memoized it computes, so a cached read never
        substitutes an empty answer for a real one.

        *limit* stops the walk early when the caller only needs to know whether
        any row exists.
        """
        ...

    async def fetch_records(self, refs: list[dict]) -> dict:
        """Everything the store knows about these entities, in one message.

        The read a query cannot express. A query names predicates, so it can ask
        about a field it knows about; a projection rendering "everything known
        about this entity" cannot name the fields, because extensions may declare
        fields on entity types they did not define and a projection that
        enumerated what it knew would silently drop exactly those.

        *refs* is a list and the reply is positional, so a projection over forty
        entities costs one message rather than forty. Prefer this over reaching
        for the store: a read that does not come through the WM contributes
        nothing to what the WM knows the answer depended on, so it is never
        invalidated when the world moves.
        """
        ...
