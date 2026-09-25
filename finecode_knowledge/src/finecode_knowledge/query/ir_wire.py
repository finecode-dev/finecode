"""Canonical JSON for the IR: terms, literals, bodies, clauses, predicates.

Split out of ``query/serialize.py`` because three things need it and only one of
them is transport. ``serialize.py`` layers ``Query`` and ``Result`` on top;
``query/snapshot.py`` uses the predicate half to ship bodies; and
``query/version.py`` hashes the same encoding to get R8's rule/predicate code
version. A leaf module keeps that last one from importing the transport layer,
which would close an import cycle through ``query/query.py``.

**Canonical, not merely serializable.** ``Var`` compares by identity, so the
encoding names each variable by first appearance -- ``_0``, ``_1``, ... Two
structurally identical bodies built from different ``Var`` objects therefore
produce byte-identical JSON, which is what lets the same encoding serve as a
memo-key input (R8) and as a wire format (FR9) without a second implementation
that could disagree with it.
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import Provenance
from finecode_knowledge.model.literal import (
    Clause,
    Conjunction,
    Literal,
    LiteralKind,
    Predicate,
)
from finecode_knowledge.model.wire import (
    prov_from_json,
    prov_to_json,
    ref_from_json,
    ref_to_json,
)
from finecode_knowledge.query.terms import Prov, Var

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = [
    "QueryWireError",
    "TermTable",
    "clause_from_json",
    "clause_to_json",
    "conjunction_from_json",
    "conjunction_to_json",
    "literal_from_json",
    "literal_to_json",
    "predicate_from_json",
    "predicate_to_json",
    "term_from_json",
    "term_to_json",
    "type_from_name",
    "value_from_json",
    "value_to_json",
]

_SCALARS: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}


class QueryWireError(SchemaError):
    """A wire form this module cannot read, named precisely enough to act on."""


# ---- values (row cells, and constants in term position) ----------------


def value_to_json(value: object) -> dict:
    """One row cell, or one constant term.

    Tagged rather than bare, because a row cell is genuinely a union: a rule
    projects entity references, scalars and whole ``Provenance`` objects (FR5)
    into the same tuple, and an untagged encoding would have to guess on the way
    back.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return {"k": "scalar", "v": value}
    if isinstance(value, EntityRef):
        return {"k": "ref", "v": ref_to_json(value)}
    if isinstance(value, Provenance):
        return {"k": "prov", "v": prov_to_json(value)}
    raise QueryWireError(
        f"Cannot serialize a {type(value).__name__} as a query value. Rows carry "
        "scalars, entity references and provenance; anything else has no wire form."
    )


def value_from_json(data: dict) -> object:
    kind = data["k"]
    if kind == "scalar":
        return data["v"]
    if kind == "ref":
        return ref_from_json(data["v"])
    if kind == "prov":
        return prov_from_json(data["v"])
    raise QueryWireError(f"Unknown query value kind: {kind!r}")


# ---- terms -------------------------------------------------------------


def _type_name(type_: object) -> str | None:
    """How a ``Var``'s declared type is spelled, or ``None`` if it is not.

    An entity type is spelled by its **qualified registry name**, so the reader
    resolves it the same way it resolves everything else. A JSON scalar is
    spelled by its builtin name. Anything else -- ``object``, a union, a class
    the reader has never heard of -- is omitted rather than approximated: the
    declared type is a construction-time checking aid (§5.8), the walk never
    reads it, and reconstructing unions would mean shipping a type language.
    """
    qualified = getattr(type_, "qualified_name", None)
    if callable(qualified) and isinstance(type_, type):
        return typing.cast(str, qualified())
    if isinstance(type_, type) and type_.__name__ in _SCALARS:
        return type_.__name__
    return None


def type_from_name(name: str | None, schema: SchemaRegistry) -> object:
    if name is None:
        return object
    if name in _SCALARS:
        return _SCALARS[name]
    try:
        return schema.entity_type(name)
    except SchemaError:
        # An entity type the reader's registry does not hold. The walk never
        # reads a var's type, so this is recoverable -- and losing it loudly
        # would refuse a query the reader can in fact answer.
        return object


class TermTable:
    """Assigns each ``Var`` its first-appearance name, and rebuilds by that name."""

    def __init__(self) -> None:
        self._names: dict[int, str] = {}
        self._vars: dict[str, Var] = {}

    def name_of(self, term: Var) -> str:
        held = self._names.get(id(term))
        if held is None:
            held = f"_{len(self._names)}"
            self._names[id(term)] = held
        return held

    def var_for(self, name: str, type_: object, *, prov: bool) -> Var:
        held = self._vars.get(name)
        if held is None:
            held = Prov() if prov else Var(type_)
            self._vars[name] = held
        return held


def term_to_json(term: object, table: TermTable) -> dict:
    if isinstance(term, Prov):
        return {"k": "prov_var", "n": table.name_of(term)}
    if isinstance(term, Var):
        data: dict = {"k": "var", "n": table.name_of(term)}
        type_name = _type_name(term.type)
        if type_name is not None:
            data["t"] = type_name
        return data
    return value_to_json(term)


def term_from_json(data: dict, table: TermTable, schema: SchemaRegistry) -> object:
    kind = data["k"]
    if kind == "prov_var":
        return table.var_for(data["n"], object, prov=True)
    if kind == "var":
        return table.var_for(
            data["n"], type_from_name(data.get("t"), schema), prov=False
        )
    return value_from_json(data)


# ---- literals and bodies -----------------------------------------------


def literal_to_json(literal: Literal, table: TermTable | None = None) -> dict:
    table = table if table is not None else TermTable()
    data: dict = {
        "kind": literal.kind.value,
        "predicate": literal.predicate,
        "terms": [term_to_json(t, table) for t in literal.terms],
    }
    if literal.entity_type is not None:
        data["entity_type"] = literal.entity_type
    if literal.key_fields:
        data["key_fields"] = list(literal.key_fields)
    if literal.at is not None:
        data["at"] = term_to_json(literal.at, table)
    if literal.negated:
        data["negated"] = True
    return data


def literal_from_json(
    data: dict, schema: SchemaRegistry, table: TermTable | None = None
) -> Literal:
    table = table if table is not None else TermTable()
    return Literal(
        kind=LiteralKind(data["kind"]),
        predicate=data["predicate"],
        terms=tuple(term_from_json(t, table, schema) for t in data["terms"]),
        entity_type=data.get("entity_type"),
        key_fields=tuple(data.get("key_fields", ())),
        at=None if "at" not in data else term_from_json(data["at"], table, schema),
        negated=bool(data.get("negated", False)),
    )


def conjunction_to_json(
    body: Conjunction, table: TermTable | None = None
) -> list[dict]:
    table = table if table is not None else TermTable()
    return [literal_to_json(literal, table) for literal in body.literals]


def conjunction_from_json(
    data: list[dict], schema: SchemaRegistry, table: TermTable | None = None
) -> Conjunction:
    table = table if table is not None else TermTable()
    return Conjunction(
        literals=tuple(literal_from_json(d, schema, table) for d in data)
    )


# ---- clauses and predicates --------------------------------------------


def clause_to_json(clause: Clause) -> dict:
    """One clause, with its own private term table.

    A clause's variables are renamed apart at expansion anyway
    (``interpret._rename``), so numbering them per clause rather than per
    predicate is not just harmless -- it is what makes two structurally identical
    clauses encode identically. The head is named first, so ``_0``, ``_1``, ...
    are the head parameters in order.
    """
    table = TermTable()
    head = [term_to_json(term, table) for term in clause.head]
    return {"head": head, "body": conjunction_to_json(clause.body, table)}


def clause_from_json(data: dict, schema: SchemaRegistry) -> Clause:
    table = TermTable()
    head = tuple(term_from_json(term, table, schema) for term in data["head"])
    return Clause(head=head, body=conjunction_from_json(data["body"], schema, table))


def predicate_to_json(predicate: Predicate) -> dict:
    return {
        "id": predicate.id,
        "params": list(predicate.params),
        "clauses": [clause_to_json(clause) for clause in predicate.clauses],
    }


def predicate_from_json(data: dict, schema: SchemaRegistry) -> Predicate:
    return Predicate(
        id=data["id"],
        params=tuple(data["params"]),
        clauses=tuple(clause_from_json(c, schema) for c in data["clauses"]),
    )
