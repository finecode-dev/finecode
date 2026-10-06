"""``Query`` on the wire (FR9, ``python-api-design.md`` §5.9).

**Why this is on the memo DAG's critical path.** ``goals.md`` §4.10 decides that
the ER hosts rule *code* while the query executes in the WM. The query is
therefore the thing that crosses the process boundary -- one query out, one
result back -- and until it could be serialized, "the WM owns the store" was a
statement no code could act on.

The IR encoding itself lives in ``query/ir_wire.py``; this module is the
``Query``/``Result`` layer on top of it, and re-exports the pieces callers of
this module already reach for.

## What crosses, and what deliberately does not

**A derived predicate crosses by registered name, never by inlining its body**
(§5.9). A serialized query is a *reference into the registry*: the receiver
resolves ``predicate`` through its own ``SchemaRegistry`` and expands the body it
finds there. Two things follow, and the second is the reason:

- the wire form stays small and readable no matter how deep the vocabulary goes;
- **rule and predicate code version stays a distinct memo-key component** (R8).
  Inlining the body would fold the definition into the query's identity, and the
  memo key could no longer tell "the same question about changed rules" from "a
  different question". ``memo/keys.py`` puts the referenced predicates' version
  hashes into the node key instead, which is the same information carried where
  it belongs.

The bodies themselves cross **once**, in the registry snapshot
(``query/snapshot.py``), because the executing side has to expand them.

**The schema does not cross with the query.** ``Query.schema`` is
``compare=False`` and is not serialized; ``query_from_json`` takes the receiver's
registry. That is the schema rule restated at the value level: the WM is
*given* a schema once, not handed one per message.

## Variables

``Var`` compares by **identity**, so the wire form names each one by first
appearance -- ``_0``, ``_1``, ... -- and rebuilding produces fresh ``Var``
objects wired up the same way. Two structurally identical queries therefore
serialize identically, which is what makes the wire form usable as a memo key
input at all.

A variable's declared *type* round-trips **lossily and on purpose**: an entity
type survives (it is a registry name), a value type survives if it is one of the
handful of JSON scalars, and anything else -- a union, an arbitrary class --
comes back as ``object``. The declared type is a construction-time checking aid
(§5.8) and validation has already run on the sending side; the walk itself never
reads it. Reconstructing unions faithfully would mean shipping a type language,
to protect a check that has already happened.
"""

from __future__ import annotations

import typing

from finecode_knowledge.query.freshness import (
    Freshness,
    Reservation,
    ReservationKind,
    Revision,
)
from finecode_knowledge.query.ir_wire import (
    QueryWireError,
    TermTable,
    clause_from_json,
    clause_to_json,
    conjunction_from_json,
    conjunction_to_json,
    literal_from_json,
    literal_to_json,
    predicate_from_json,
    predicate_to_json,
    term_from_json,
    term_to_json,
    value_from_json,
    value_to_json,
)
from finecode_knowledge.query.query import Query, Result

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = [
    "QueryWireError",
    "clause_from_json",
    "clause_to_json",
    "conjunction_from_json",
    "conjunction_to_json",
    "freshness_from_json",
    "freshness_to_json",
    "literal_from_json",
    "literal_to_json",
    "predicate_from_json",
    "predicate_to_json",
    "query_from_json",
    "query_to_json",
    "result_from_json",
    "result_to_json",
    "rows_from_json",
    "rows_to_json",
    "value_from_json",
    "value_to_json",
]

QUERY_WIRE_VERSION = 1


def rows_to_json(rows: typing.Iterable[tuple[object, ...]]) -> list[list[dict]]:
    return [[value_to_json(cell) for cell in row] for row in rows]


def rows_from_json(data: list[list[dict]]) -> list[tuple[object, ...]]:
    return [tuple(value_from_json(cell) for cell in row) for row in data]


# ---- the query itself --------------------------------------------------


def query_to_json(query: Query) -> dict:
    """*query* as JSON. The schema is **not** included -- see the module docstring.

    The projection is serialized first so its variables take the low numbers,
    which makes the wire form readable and makes the row order it implies
    obvious to anyone reading a captured message.
    """
    table = TermTable()
    projection = [term_to_json(term, table) for term in query.projection]
    return {
        "v": QUERY_WIRE_VERSION,
        "projection": projection,
        "body": conjunction_to_json(query.body, table),
    }


def query_from_json(data: dict, schema: SchemaRegistry) -> Query:
    """Rebuild a ``Query`` against *schema*.

    **No re-validation.** §5.8's checks ran when the query was *constructed*, on
    the side that has the rule source and can name the offending line; running
    them again here would report a schema error against a wire message. What this
    side does check is that the wire form is a wire form it understands.

    Raises:
        QueryWireError: *data* is not a query this version can read.
    """
    version = data.get("v")
    if version != QUERY_WIRE_VERSION:
        raise QueryWireError(
            f"Unsupported serialized-query version {version!r}; this build reads "
            f"version {QUERY_WIRE_VERSION}."
        )
    table = TermTable()
    projection = tuple(term_from_json(t, table, schema) for t in data["projection"])
    return Query(
        projection=projection,
        body=conjunction_from_json(data["body"], schema, table),
        schema=schema,
    )


# ---- the result that comes back ----------------------------------------


def freshness_to_json(freshness: Freshness) -> dict:
    return {
        "revision": str(freshness.revision),
        "reservations": [
            {"kind": r.kind.value, "subject": r.subject, "detail": r.detail}
            for r in freshness.reservations
        ],
    }


def freshness_from_json(data: dict) -> Freshness:
    return Freshness(
        revision=Revision(data["revision"]),
        reservations=tuple(
            Reservation(
                kind=ReservationKind(r["kind"]),
                subject=r["subject"],
                detail=r["detail"],
            )
            for r in data["reservations"]
        ),
    )


def result_to_json(result: Result[list[tuple[object, ...]]]) -> dict:
    """Rows **and** the verdict, always (R16).

    The footprint does not ride along, and that is ADR-0013 D5 holding across the
    boundary rather than an omission: the footprint is recorded where the reads
    happen, which is now the WM, and it is handed to the memo layer there. The ER
    could not record one if it wanted to, because it does not read facts (R7/R21).
    """
    return {
        "rows": rows_to_json(result.value),
        "freshness": freshness_to_json(result.freshness),
    }


def result_from_json(data: dict) -> Result[list[tuple[object, ...]]]:
    return Result(
        value=rows_from_json(data["rows"]),
        freshness=freshness_from_json(data["freshness"]),
    )
