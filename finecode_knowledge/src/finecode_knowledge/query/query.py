"""``Query``, ``Result`` and the async terminals (ADR-0006, ADR-0016).

A ``Query`` is **inert until a terminal runs**. ``__iter__`` / ``__len__`` /
``__bool__`` do not execute and raise an error naming the missing terminal
(NFR6) -- an un-awaited coroutine is then a second, independent way the same
mistake surfaces loudly.

**Terminals are ``async``, with no sync twins** (ADR-0016 D1). The ER hosts rule
code while the query executes in the WM, so a terminal crosses a process
boundary on the server path. One spelling, because adding a spelling is additive
and removing one is breaking: v1 takes the option that leaves the other
reachable, and the sync path is the one that expires.
"""

from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, Literal, Term
from finecode_knowledge.model.registry import default_registry
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.freshness import Freshness
from finecode_knowledge.query.validate import validate_body

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.query.backend import Backend

__all__ = ["Mode", "Query", "QueryNotExecutedError", "Result", "query"]

T = typing.TypeVar("T")


class QueryNotExecutedError(TypeError):
    """Raised when an unexecuted ``Query`` is used as a value (ADR-0006).

    A ``TypeError`` rather than a bespoke base, because that is what Python
    raises for "this object is not iterable" and the mistake is exactly that.
    """


@dataclasses.dataclass(frozen=True)
class Result(typing.Generic[T]):
    """Rows plus a freshness verdict. Both, always (R16)."""

    value: T
    freshness: Freshness

    @property
    def rows(self) -> T:
        """The answer. **Unguarded, deliberately** (ADR-0014).

        Nothing forces a caller to read ``.freshness``, and a guarded
        ``unwrap()`` was rejected rather than overlooked: every file-loaded
        result carries one ``UNTRACKED`` reservation (D7), so a ``rows`` that
        raised unless verified would raise on the first call anyone made. The
        escape hatch would be reached for immediately and then by habit,
        leaving the API with ceremony and no enforcement.

        R16 is delivered instead by the verdict always existing and by output
        surfaces rendering it. The guard becomes viable once persisted input
        fingerprints (R11) let the file-loaded path *confirm* rather than
        reserve -- attempting it before then is the sequencing error to avoid.
        """
        return self.value


@dataclasses.dataclass(frozen=True)
class Query:
    """A reified, unexecuted conjunction plus a projection list."""

    projection: tuple[Term, ...]
    body: Conjunction
    schema: SchemaRegistry = dataclasses.field(compare=False, repr=False)

    def where(self, *literals: Literal) -> Query:
        """Add literals to the body, validating the whole thing (§5.8).

        Returns a new ``Query``: the IR is frozen, so building is a chain of
        values rather than mutation of one.
        """
        body = Conjunction(literals=(*self.body.literals, *literals))
        validated = dataclasses.replace(self, body=body)
        validate_body(body, self.schema, context="query", projected=self.projection)
        return validated

    def order_by(self, *terms: Term) -> OrderedQuery:
        """Request a deterministic row order (NFR8).

        Query results are sets and their order is unspecified unless asked for.
        Violations are the exception -- the ``ViolationBuilder`` sorts them
        canonically, so a gate's output is stable without any rule calling this.
        """
        for term in terms:
            if term not in self.projection:
                raise SchemaError(
                    f"order_by term {term!r} is not in the projection "
                    f"({', '.join(map(repr, self.projection))})."
                )
        return OrderedQuery(query=self, keys=terms)

    # ---- terminals ----------------------------------------------------

    async def all(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[list[tuple[object, ...]]]:
        result = await backend.run(self, mode=mode)
        return Result(value=list(result.value), freshness=result.freshness)

    async def one(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[tuple[object, ...]]:
        result = await backend.run(self, mode=mode)
        rows = list(result.value)
        if len(rows) != 1:
            raise SchemaError(f"one() expected exactly 1 row, got {len(rows)}.")
        return Result(value=rows[0], freshness=result.freshness)

    async def exists(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[bool]:
        result = await backend.run(self, mode=mode, limit=1)
        return Result(value=bool(list(result.value)), freshness=result.freshness)

    async def count(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[int]:
        result = await backend.run(self, mode=mode)
        return Result(value=len(list(result.value)), freshness=result.freshness)

    # ---- non-execution (ADR-0006) -------------------------------------

    def __iter__(self) -> typing.NoReturn:
        raise QueryNotExecutedError(
            "A Query is inert until a terminal runs it. Write "
            "`(await query.all(backend)).rows` rather than iterating the query."
        )

    def __len__(self) -> typing.NoReturn:
        raise QueryNotExecutedError(
            "A Query has no length until a terminal runs it. Write "
            "`(await query.count(backend)).value`."
        )

    def __bool__(self) -> typing.NoReturn:
        raise QueryNotExecutedError(
            "A Query has no truth value until a terminal runs it. Write "
            "`(await query.exists(backend)).value`."
        )


@dataclasses.dataclass(frozen=True)
class OrderedQuery:
    """A query plus an explicit row order (NFR8)."""

    query: Query
    keys: tuple[Term, ...]

    async def all(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[list[tuple[object, ...]]]:
        result = await self.query.all(backend, mode=mode)
        positions = [self.query.projection.index(key) for key in self.keys]
        ordered = sorted(
            result.value, key=lambda row: tuple(str(row[p]) for p in positions)
        )
        return Result(value=ordered, freshness=result.freshness)


def query(*projection: Term, schema: SchemaRegistry | None = None) -> Query:
    """Start a query projecting *projection*. Add literals with ``.where(...)``."""
    if schema is None:
        schema = default_registry()
    return Query(projection=projection, body=Conjunction(literals=()), schema=schema)
