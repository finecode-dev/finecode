"""``Query`` and its result on the wire (FR9, §5.9).

The property under test is **not** ``from_json(to_json(x)) == x``: ``Var``
compares by identity, so a rebuilt query is never equal to the original and an
equality round-trip would be asserting the wrong thing. The property that
matters is that the wire form is *canonical* -- ``to_json(from_json(w)) == w``,
and two structurally identical queries built from different ``Var`` objects
serialize identically. That is what lets the wire form be a memo-key input (R8)
rather than merely a transport.
"""

from __future__ import annotations

import json

import pytest
from libcat import (
    LIBCAT_SCHEMA,
    Author,
    AuthorFields,
    Book,
    BookFields,
    Copy,
    Rel,
    Shelf,
)

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import Provenance, RunStamp, SourceLoc
from finecode_knowledge.query.freshness import (
    Freshness,
    Reservation,
    ReservationKind,
    Revision,
)
from finecode_knowledge.query.serialize import (
    QueryWireError,
    freshness_from_json,
    freshness_to_json,
    query_from_json,
    query_to_json,
    result_from_json,
    result_to_json,
    value_from_json,
    value_to_json,
)


def _joined() -> q.Query:
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    return q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )


# ---- the canonical form ------------------------------------------------


def test_the_wire_form_is_canonical_under_a_round_trip() -> None:
    wire = query_to_json(_joined())

    assert query_to_json(query_from_json(wire, LIBCAT_SCHEMA)) == wire


def test_two_structurally_identical_queries_serialize_identically() -> None:
    """Built from different ``Var`` objects, which compare by identity. If the wire
    form carried that identity, R8's memo key would invalidate itself on every import."""
    assert query_to_json(_joined()) == query_to_json(_joined())


def test_a_variable_is_named_by_first_appearance() -> None:
    """``_0``, ``_1``, ... with the projection numbered first, so a captured message
    reads in the order the rule author wrote."""
    wire = query_to_json(_joined())

    assert [term["n"] for term in wire["projection"]] == ["_0", "_1"]
    assert wire["body"][0]["terms"][0]["n"] == "_0", "the projected author, reused"
    assert wire["body"][0]["terms"][1]["n"] == "_2", (
        "the join variable, first seen here"
    )


def test_the_wire_form_is_json() -> None:
    """Not a formality: it crosses a JSON-RPC boundary, so anything unserializable
    fails at the transport rather than here, where the message is worse."""
    assert json.loads(json.dumps(query_to_json(_joined())))


# ---- what crosses, and what does not -----------------------------------


def test_a_derived_predicate_crosses_by_name_never_by_inlined_body() -> None:
    """§5.9, and R8's reason for it: inlining the body would fold the predicate's
    *definition* into the query's identity, and the memo key could no longer tell "the
    same question about changed rules" from "a different question"."""
    predicate = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book")
    author, shelf = q.var(Author), q.var(Shelf)

    wire = query_to_json(q.query(author).where(predicate(author, shelf)))

    (literal,) = wire["body"]
    assert literal["kind"] == "derived"
    assert literal["predicate"] == "libcat.shelves_a_book"
    assert "clauses" not in literal and "body" not in literal


def test_the_schema_does_not_ride_along_with_the_query() -> None:
    """D-7 at the value level: the receiver is *given* a registry once, not handed one
    per message."""
    wire = query_to_json(_joined())

    assert set(wire) == {"v", "projection", "body"}


def test_rebuilding_binds_the_receivers_schema() -> None:
    rebuilt = query_from_json(query_to_json(_joined()), LIBCAT_SCHEMA)

    assert rebuilt.schema is LIBCAT_SCHEMA


# ---- every literal shape survives --------------------------------------


def test_a_key_literal_keeps_its_entity_type_and_component_order() -> None:
    """``key_fields`` is ordered by KEY declaration, and the construct direction hands
    them to ``ref()`` positionally -- so losing the order would build wrong references."""
    copy = q.var(Copy)
    wire = query_to_json(
        q.query(copy).where(Copy.key(copy, branch="north", isbn="a-1"))
    )

    (literal,) = wire["body"]
    assert literal["kind"] == "key"
    assert literal["entity_type"] == Copy.qualified_name()
    assert literal["key_fields"] == ["isbn", "branch"]


def test_a_negated_literal_stays_negated() -> None:
    author, book = q.var(Author), q.var(Book)
    built = q.query(author).where(
        Rel.wrote(author, book), q.not_(Rel.borrowed(author, book))
    )

    rebuilt = query_from_json(query_to_json(built), LIBCAT_SCHEMA)

    assert [literal.negated for literal in rebuilt.body.literals] == [False, True]


def test_a_bound_provenance_term_survives_as_a_prov_not_an_ordinary_var() -> None:
    """A ``Prov`` is a distinct term kind (ADR-0010). Rebuilding it as a plain ``Var``
    would still *execute*, and would silently lose the distinction the rule head
    depends on."""
    from finecode_knowledge.query.terms import Prov

    author, shelf, at = q.var(Author), q.var(Shelf), q.Prov()
    built = q.query(at).where(Rel.frequents(author, shelf, at=at))

    rebuilt = query_from_json(query_to_json(built), LIBCAT_SCHEMA)

    assert isinstance(rebuilt.projection[0], Prov)
    assert rebuilt.body.literals[0].at is rebuilt.projection[0], (
        "the same variable, not two that happen to share a name"
    )


def test_a_constant_in_term_position_survives() -> None:
    book = q.var(Book)
    built = q.query(book).where(BookFields.isbn(book, "a-1"))

    rebuilt = query_from_json(query_to_json(built), LIBCAT_SCHEMA)

    assert rebuilt.body.literals[0].terms[1] == "a-1"


def test_an_entity_reference_in_term_position_survives() -> None:
    author = Author.ref(handle="ana")
    book = q.var(Book)
    built = q.query(book).where(Rel.wrote(author, book))

    rebuilt = query_from_json(query_to_json(built), LIBCAT_SCHEMA)

    assert rebuilt.body.literals[0].terms[0] == author


# ---- the lossy part, stated ---------------------------------------------


def test_an_entity_typed_variable_keeps_its_type() -> None:
    """Spelled by qualified registry name, so the receiver resolves it the way it
    resolves everything else."""
    rebuilt = query_from_json(query_to_json(_joined()), LIBCAT_SCHEMA)

    assert rebuilt.projection[0].type is Author
    assert rebuilt.projection[1].type is str


def test_a_variable_of_an_unrepresentable_type_comes_back_as_object() -> None:
    """A union has no wire spelling, and rebuilding one would mean shipping a type
    language to protect a check (§5.8) that already ran on the sending side."""
    src = q.Var(Shelf | Author)  # type: ignore[arg-type]
    built = q.query(src).where(Rel.frequents(src, q.var(Shelf)))

    rebuilt = query_from_json(query_to_json(built), LIBCAT_SCHEMA)

    assert rebuilt.projection[0].type is object


def test_a_wire_form_from_a_future_version_is_refused_by_name() -> None:
    wire = query_to_json(_joined())
    wire["v"] = 99

    with pytest.raises(QueryWireError, match="version 99"):
        query_from_json(wire, LIBCAT_SCHEMA)


# ---- rows and the verdict -----------------------------------------------


def test_a_row_cell_is_tagged_because_a_row_is_a_union() -> None:
    """A rule projects entity refs, scalars and whole ``Provenance`` objects into one
    tuple (FR5); an untagged encoding would have to guess on the way back."""
    prov = Provenance(
        band=Band.DECLARED,
        provider="libcat.catalog_scan",
        run=RunStamp(id="r", observed_at="t"),
        location=SourceLoc(project="p", file="catalog.toml", line=3),
    )

    for value in (Author.ref(handle="ana"), "a-1", 7, None, True, prov):
        assert value_from_json(value_to_json(value)) == value


def test_a_value_with_no_wire_form_says_so_rather_than_degrading() -> None:
    with pytest.raises(QueryWireError, match="Cannot serialize"):
        value_to_json(object())


def test_the_verdict_crosses_with_the_rows() -> None:
    """R16: both, always. A result that arrived without its verdict would be a bare
    value where staleness was possible."""
    freshness = Freshness(
        revision=Revision("rev-1"),
        reservations=(
            Reservation(ReservationKind.STALE, "libcat.catalog_scan/a.toml", "changed"),
        ),
    )
    result = q.Result(value=[(Author.ref(handle="ana"), "a-1")], freshness=freshness)

    restored = result_from_json(result_to_json(result))

    assert restored.rows == result.rows
    assert restored.freshness == freshness


def test_every_reservation_kind_survives_the_round_trip() -> None:
    """The kinds are closed and each traces to a requirement, so a wire form that
    silently dropped one would drop a requirement with it."""
    freshness = Freshness(
        revision=Revision("rev-1"),
        reservations=tuple(
            Reservation(kind, f"subject-{kind.value}", "why")
            for kind in ReservationKind
        ),
    )

    assert freshness_from_json(freshness_to_json(freshness)) == freshness


def test_a_footprint_does_not_ride_the_result() -> None:
    """ADR-0013 D5 across the boundary: the footprint is recorded where the reads
    happen -- WM-side -- and handed to the memo layer there. The ER could not record
    one if it wanted to, because it does not read facts (R7/R21)."""
    result = q.Result(
        value=[], freshness=Freshness(revision=Revision("rev-1"), reservations=())
    )

    assert set(result_to_json(result)) == {"rows", "freshness"}


def test_a_row_binding_a_provenance_arrives_with_its_location() -> None:
    """The location is most of a violation's value, so losing it in transit would make
    the WM path produce visibly worse diagnostics than the in-ER one."""
    loc = SourceLoc(project="ana", file="catalog.toml", line=7)
    prov = Provenance(
        band=Band.DECLARED,
        provider="libcat.catalog_scan",
        run=RunStamp(id="r", observed_at="t"),
        location=loc,
    )
    result = q.Result(
        value=[(prov,)], freshness=Freshness(revision=Revision("r"), reservations=())
    )

    (restored_row,) = result_from_json(result_to_json(result)).rows

    assert isinstance(restored_row[0], Provenance)
    assert restored_row[0].location == loc


def test_an_unqualified_field_literal_is_not_something_the_wire_can_invent() -> None:
    """Sanity on the qualified-name discipline surviving transport: every predicate on
    the wire is qualified, because every predicate in the IR already was."""
    wire = query_to_json(
        q.query(a := q.var(Author)).where(AuthorFields.handle(a, q.var(str)))
    )

    assert wire["body"][0]["predicate"] == "libcat.handle"
    assert wire["body"][0]["entity_type"] == "libcat.Author"
