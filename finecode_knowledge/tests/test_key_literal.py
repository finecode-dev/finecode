"""The `KEY` literal: addressing, not assertion (ADR-0019).

The defect this closes was two instances of one hole in a single rule body,
failing in **opposite** directions:

- the field spelling bound nothing for an entity nobody scanned -- which is
  exactly what such a rule reports -- so the rule could not fire on its own
  subject matter.
- inside `q.not_(...)` the same hole failed *open*: a relationship that was
  declared but whose target was not separately scanned read as absent, and the
  rule reported a violation that did not exist.

Written against ``libcat`` (``tests/fixtures/libcat``), whose ``Copy`` type has a
**composite** KEY -- a single-field key exercises neither the ordering rule nor
the partial-key refusal (D2/D6).
"""

from __future__ import annotations

import pytest
from libcat import (
    LIBCAT_SCHEMA,
    Author,
    Book,
    BookFields,
    Copy,
    Rel,
    Shelf,
    ShelfFields,
)

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import EdgeFact, FieldFact, Provenance, RunStamp
from finecode_knowledge.model.literal import LiteralKind
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.validate import (
    UnconstrainedKeyBindingWarning,
    validate_body,
)

_SHELVES = "libcat.shelf_survey"
_CATALOG = "libcat.catalog_scan"


def _prov(provider: str = _SHELVES) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=RunStamp(id="r", observed_at="2026-07-30T00:00:00Z"),
        location=None,
    )


@pytest.fixture
def store() -> FactStore:
    """``s-A`` is surveyed; ``s-B`` is referenced by an edge and nothing else.

    That asymmetry is the whole point: a survey emits fields only for the shelf it
    actually walked, and reaches neighbouring ones through an edge alone.
    """
    fact_store = FactStore(LIBCAT_SCHEMA)
    fact_store.ingest(
        _SHELVES,
        [
            FieldFact(
                entity=Shelf.ref(code="s-A"), field="code", value="s-A", prov=_prov()
            ),
            EdgeFact(
                kind="adjacent_to",
                src=Shelf.ref(code="s-A"),
                dst=Shelf.ref(code="s-B"),
                prov=_prov(),
            ),
        ],
    )
    return fact_store


@pytest.fixture
def backend(store: FactStore) -> InterpreterBackend:
    return InterpreterBackend(store)


# ---- D1: the literal ---------------------------------------------------


def test_key_names_the_entity_type_and_the_fields_in_key_order() -> None:
    literal = Copy.key(q.var(Copy), branch="north", isbn="a-1")

    assert literal.kind is LiteralKind.KEY
    assert literal.entity_type == Copy.qualified_name()
    assert literal.key_fields == ("isbn", "branch"), (
        "components are ordered by KEY declaration, not by keyword order, so the "
        "construct direction can hand them to ref() positionally"
    )


def test_key_accepts_any_subset_of_the_key() -> None:
    literal = Copy.key(q.var(Copy), branch=q.var(str))

    assert literal.key_fields == ("branch",)


def test_key_rejects_a_field_that_is_not_part_of_the_key() -> None:
    """``title`` is asserted about a Book, not part of its identity. Asking for it
    through the addressing spelling is the inverse of the defect ADR-0019 closes."""
    with pytest.raises(SchemaError, match="not part of its KEY"):
        Book.key(q.var(Book), title="Moby-Dick")


def test_key_takes_no_provenance() -> None:
    """There is no fact, so there is nothing for ``at=`` to bind (D4)."""
    with pytest.raises(SchemaError, match="cannot bind provenance"):
        Shelf.key(q.var(Shelf), code=q.var(str), at=q.Prov())


# ---- D2: the three directions -----------------------------------------


async def test_project_direction_reads_the_key_off_a_bound_reference(
    backend: InterpreterBackend,
) -> None:
    """The case a rule about *referenced-but-unscanned* entities needs: ``s-B`` has no
    facts, and its code is still readable because it is in the reference."""
    near, far, missing = q.var(Shelf), q.var(Shelf), q.var(str)

    result = await (
        q.query(missing)
        .where(Rel.adjacent_to(near, far), Shelf.key(far, code=missing))
        .all(backend)
    )

    assert result.rows == [("s-B",)]


async def test_the_field_spelling_still_means_scanned_and_finds_nothing(
    backend: InterpreterBackend,
) -> None:
    """D4: ``ShelfFields.code`` stays a fact scan. "Every shelf some survey names" and
    "every shelf we actually walked" are different sets, and keeping both expressible
    is why the KEY literal is a separate spelling rather than a branch inside
    ``_match_field``."""
    near, far, missing = q.var(Shelf), q.var(Shelf), q.var(str)

    result = await (
        q.query(missing)
        .where(Rel.adjacent_to(near, far), ShelfFields.code(far, missing))
        .all(backend)
    )

    assert result.rows == []


async def test_construct_direction_builds_the_reference_from_a_complete_key(
    backend: InterpreterBackend,
) -> None:
    """Exactly what a hand-written ``Book.ref(isbn=...)`` did, expressed in the IR."""
    book = q.var(Book)

    result = await q.query(book).where(Book.key(book, isbn="anything")).all(backend)

    assert result.rows == [(Book.ref(isbn="anything"),)]


async def test_construct_direction_works_for_a_composite_key(
    backend: InterpreterBackend,
) -> None:
    copy = q.var(Copy)

    result = await (
        q.query(copy).where(Copy.key(copy, isbn="a-1", branch="north")).all(backend)
    )

    assert result.rows == [(Copy.ref(isbn="a-1", branch="north"),)]


async def test_test_direction_survives_only_when_the_key_agrees(
    backend: InterpreterBackend,
) -> None:
    near, far = q.var(Shelf), q.var(Shelf)

    agrees = await (
        q.query(far)
        .where(Rel.adjacent_to(near, far), Shelf.key(far, code="s-B"))
        .all(backend)
    )
    disagrees = await (
        q.query(far)
        .where(Rel.adjacent_to(near, far), Shelf.key(far, code="other"))
        .all(backend)
    )

    assert agrees.rows == [(Shelf.ref(code="s-B"),)]
    assert disagrees.rows == []


async def test_a_partial_key_with_an_unbound_entity_raises_naming_what_is_missing(
    backend: InterpreterBackend,
) -> None:
    """D6. Binding an entity from a partial key would mean enumerating every reference
    the store has seen including endpoints carrying no facts -- a new seam member with
    no caller yet, so it raises rather than guessing."""
    copy = q.var(Copy)

    with pytest.raises(SchemaError, match="partial key"):
        await q.query(copy).where(Copy.key(copy, branch="north")).all(backend)


# ---- D3: identity, not existence --------------------------------------


async def test_a_key_literal_does_not_assert_that_the_entity_is_known(
    backend: InterpreterBackend,
) -> None:
    """The substantive semantic change. ``Book.key(book, isbn=...)`` binds a book the
    store may know nothing about; the *next* literal is what constrains it."""
    book, author = q.var(Book), q.var(Author)

    addressed = await q.query(book).where(Book.key(book, isbn="ghost")).all(backend)
    constrained = await (
        q.query(book)
        .where(Book.key(book, isbn="ghost"), Rel.wrote(author, book))
        .all(backend)
    )

    assert addressed.rows == [(Book.ref(isbn="ghost"),)]
    assert constrained.rows == []


def test_a_variable_bound_only_by_a_key_literal_warns() -> None:
    book, shelf = q.var(Book), q.var(Shelf)
    body = q.all_(Shelf.key(shelf, code="x"), Book.key(book, isbn="y"))

    with pytest.warns(UnconstrainedKeyBindingWarning, match="binds identity"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=())


def test_a_key_binding_constrained_by_an_edge_does_not_warn(recwarn) -> None:
    book, author = q.var(Book), q.var(Author)
    body = q.all_(Book.key(book, isbn="y"), Rel.wrote(author, book))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=())

    assert not [
        w for w in recwarn if issubclass(w.category, UnconstrainedKeyBindingWarning)
    ]


def test_a_head_variable_bound_only_by_a_key_literal_does_not_warn(recwarn) -> None:
    """A predicate clause that addresses its own head is exactly this: the caller
    constrains the head."""
    shelf = q.var(Shelf)
    body = q.all_(Shelf.key(shelf, code="x"))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(shelf,))

    assert not [
        w for w in recwarn if issubclass(w.category, UnconstrainedKeyBindingWarning)
    ]


# ---- D5: no footprint, no conflict consultation ------------------------


async def test_a_key_literal_records_no_footprint_key(
    backend: InterpreterBackend,
) -> None:
    """It consults no slot, so the empty contribution is *precise* rather than an
    under-approximation: no stored fact can arrive that changes its answer. What
    changes when a shelf is finally surveyed is the edges -- read by ordinary literals
    that do record keys (D5)."""
    near, far, missing = q.var(Shelf), q.var(Shelf), q.var(str)

    await (
        q.query(missing)
        .where(Rel.adjacent_to(near, far), Shelf.key(far, code=missing))
        .all(backend)
    )

    assert [key for key in backend.last_footprint.keys if key[0] == "field"] == []
    assert [key[0] for key in backend.last_footprint.keys] == ["edge"]


async def test_the_negation_no_longer_fails_open_for_an_unscanned_target() -> None:
    """ADR-0019 §2, and the **behaviour change** it records.

    ``borrowed_titled`` used to be spelled with the field literal, which bound only
    when the target was independently scanned -- so a declared-but-unscanned target
    read as absent under the negation and produced a violation that did not exist. It
    now matches by identity whichever way the entity was reached.
    """
    from libcat.predicates import borrowed_titled

    fact_store = FactStore(LIBCAT_SCHEMA)
    fact_store.ingest(
        _CATALOG,
        [
            EdgeFact(
                kind="borrowed",
                src=Author.ref(handle="ana"),
                dst=Book.ref(isbn="unscanned"),
                prov=_prov(_CATALOG),
            )
        ],
    )
    author, isbn = q.var(Author), q.var(str)

    result = await (
        q.query(isbn)
        .where(borrowed_titled(author, isbn))
        .all(InterpreterBackend(fact_store))
    )

    assert result.rows == [("unscanned",)], (
        "the loan is declared; nothing catalogued the book, and that must not make it "
        "read as absent"
    )
    assert (
        list(
            fact_store.field_facts(
                Book.qualified_name(), BookFields.isbn.qualified_name
            )
        )
        == []
    )


def test_key_literals_do_not_change_the_stored_facts(store: FactStore) -> None:
    """D7: providers, the store and the seam are unchanged. ``contains`` keeps meaning
    "this reference has facts about it", so ``entity_counts_by_type`` keeps counting
    scanned entities rather than merely-referenced ones."""
    assert store.contains(Shelf.qualified_name(), Shelf.ref(code="s-A"))
    assert not store.contains(Shelf.qualified_name(), Shelf.ref(code="s-B"))
    assert list(store.entities_of_type(Shelf.qualified_name())) == [
        Shelf.ref(code="s-A")
    ]


def test_a_key_literal_against_a_reference_of_another_type_does_not_match() -> None:
    literal = Shelf.key(
        EntityRef(type=Book.qualified_name(), key=("x",)), code=q.var(str)
    )

    assert literal.entity_type == Shelf.qualified_name()
