"""The rule authoring surface -- imported as ``q``.

    from finecode_knowledge import query as q

Everything here binds to a ``SchemaRegistry`` (C1) and adds no entity, field or
relation of its own. Which registry is either passed as ``schema=`` or taken from
the one a schema package nominated with ``model.registry.set_default_registry``;
the engine holds no schema to fall back to (R20).
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.literal import Body, Conjunction, Literal
from finecode_knowledge.query.backend import Backend, Mode
from finecode_knowledge.query.freshness import Freshness, Reservation, ReservationKind
from finecode_knowledge.query.predicate import DerivedPredicate, PredicateShape, derived
from finecode_knowledge.query.query import Query, QueryNotExecutedError, Result, query
from finecode_knowledge.query.records import (
    RecordSource,
    records_from_json,
    records_to_json,
    refs_from_json,
    refs_to_json,
)
from finecode_knowledge.query.remote import (
    QueryTransport,
    RemoteBackend,
    RemoteQueryError,
)
from finecode_knowledge.query.rule import (
    Rule,
    Violation,
    ViolationBuilder,
    rule,
    template,
)
from finecode_knowledge.query.serialize import (
    QueryWireError,
    query_from_json,
    query_to_json,
    result_from_json,
    result_to_json,
)
from finecode_knowledge.query.snapshot import (
    SnapshotError,
    registry_from_json,
    registry_to_json,
)
from finecode_knowledge.query.terms import Prov, Var

__all__ = [
    "Backend",
    "Body",
    "Conjunction",
    "DerivedPredicate",
    "Freshness",
    "Literal",
    "Mode",
    "PredicateShape",
    "Prov",
    "Query",
    "QueryNotExecutedError",
    "QueryTransport",
    "QueryWireError",
    "RecordSource",
    "RemoteBackend",
    "RemoteQueryError",
    "Reservation",
    "ReservationKind",
    "Result",
    "Rule",
    "SnapshotError",
    "Var",
    "Violation",
    "ViolationBuilder",
    "all_",
    "derived",
    "not_",
    "query",
    "query_from_json",
    "query_to_json",
    "records_from_json",
    "records_to_json",
    "refs_from_json",
    "refs_to_json",
    "registry_from_json",
    "registry_to_json",
    "result_from_json",
    "result_to_json",
    "rule",
    "template",
    "var",
]

T = typing.TypeVar("T")


def var(type_: type[T] | object = object) -> Var[T]:
    """A fresh logic variable of the given type."""
    return Var(type_)


def all_(*literals: Literal) -> Conjunction:
    """A body: every literal must hold. Returns a *value* (ADR-0007)."""
    for literal in literals:
        if not isinstance(literal, Literal):
            raise TypeError(
                f"q.all_ takes literals, got {type(literal).__name__}. "
                "A literal comes from calling a Field, a Relationship or a derived predicate."
            )
    return Conjunction(literals=literals)


def not_(literal: Literal) -> Literal:
    """Negate exactly one literal, base or derived (FR3).

    There is no inline negated conjunction: an existential inside a negation is
    factored into a named predicate, which is what keeps negation a single
    literal the engine can evaluate as a membership test.
    """
    if not isinstance(literal, Literal):
        raise TypeError(
            f"q.not_ takes one literal, got {type(literal).__name__}. "
            "To negate an existential, factor it into a `@q.derived` predicate first."
        )
    if literal.negated:
        raise TypeError("q.not_ applied twice; double negation is not part of the IR.")
    return literal.negate()
