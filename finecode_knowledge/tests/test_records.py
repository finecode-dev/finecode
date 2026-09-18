"""The record read, and the existence literal that stopped it needing a second one.

Phase 1b's subject: **every** way of reading state goes through the channel that
records a footprint. Two things were missing before it, and they are different
kinds of missing.

- ``record()`` -- "everything known about this entity" -- has no query spelling
  and cannot have one, because the field set is open by R18/R19's design. So it
  became a second method on the read seam (``query/records.py``).
- ``contains()`` -- "does this entity exist at all" -- *could* have had a query
  spelling and did not, so callers left the language to ask it. That is
  ``LiteralKind.KNOWN``.

The first is a genuine second shape. The second was a gap, and the distinction
matters: a read the query language cannot express is a read somebody will express
another way, outside anything that can invalidate it.
"""

from __future__ import annotations

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp, SourceLoc
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.query import footprint as fp
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.records import records_from_json, records_to_json

AUTHORS = "libcat.author_index"
CATALOG = "libcat.catalog_scan"


def _prov(provider: str = AUTHORS, line: int = 3) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=RunStamp(id="r", observed_at="t"),
        location=SourceLoc(project="p", file="catalog.toml", line=line),
    )


@pytest.fixture
def store() -> FactStore:
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        AUTHORS,
        [
            FieldFact(
                entity=Author.ref(handle="ana"),
                field="handle",
                value="ana",
                prov=_prov(),
            ),
            FieldFact(
                entity=Author.ref(handle="ana"),
                field="display_name",
                value="Ana Reyes",
                prov=_prov(line=4),
            ),
        ],
    )
    store.ingest(
        CATALOG,
        [
            FieldFact(
                entity=Book.ref(isbn="a-1"),
                field="isbn",
                value="a-1",
                prov=_prov(CATALOG),
            )
        ],
    )
    return store


@pytest.fixture
def backend(store: FactStore) -> InterpreterBackend:
    return InterpreterBackend(store, schema=LIBCAT_SCHEMA)


# ---- the record read ---------------------------------------------------


async def test_a_record_carries_every_field_with_its_provenance(
    backend: InterpreterBackend,
) -> None:
    """The whole point: a projection renders what it was given, including fields
    it has never heard of. A query could only ask about fields it names."""
    (record,) = await backend.records([Author.ref(handle="ana")])

    assert set(record.fields) == {"libcat.handle", "libcat.display_name"}
    assert record.fields["libcat.display_name"].value == "Ana Reyes"
    assert record.fields["libcat.display_name"].prov.location.line == 4


async def test_an_unknown_entity_yields_an_empty_record_rather_than_being_dropped(
    backend: InterpreterBackend,
) -> None:
    """The reply is positional. Omitting a miss would make the result's length
    depend on the store's contents and every caller would re-zip by hand."""
    found = await backend.records(
        [Author.ref(handle="ana"), Author.ref(handle="nobody")]
    )

    assert len(found) == 2
    assert found[1].fields == {}


async def test_a_record_read_is_recorded_in_the_footprint(
    backend: InterpreterBackend,
) -> None:
    """**The reason this method exists.** Before Phase 1b the consumer held the
    ``FactSource`` and read it directly, contributing no key -- so nothing built
    on the read could ever be invalidated (R21, ADR-0013 D4.3 one layer up)."""
    refs = [Author.ref(handle="ana"), Book.ref(isbn="a-1")]

    await backend.records(refs)

    assert backend.last_footprint.keys == tuple(fp.entity_key(ref) for ref in refs)


async def test_a_missing_entitys_read_is_recorded_too(
    backend: InterpreterBackend,
) -> None:
    """ "Nothing is known about this" is an answer resting on the *absence* of
    facts, and a first fact about it is exactly what would change the answer."""
    await backend.records([Author.ref(handle="nobody")])

    assert fp.entity_key(Author.ref(handle="nobody")) in backend.last_footprint


async def test_a_record_read_costs_one_key_per_ref_not_one_per_field(
    backend: InterpreterBackend,
) -> None:
    """Criterion 8's shape for this read: the count tracks what was asked for,
    not how much came back. ``ana`` has two fields and contributes one key."""
    await backend.records([Author.ref(handle="ana")])

    assert len(backend.last_footprint) == 1


# ---- the wire form -----------------------------------------------------


async def test_records_round_trip_through_the_wire(
    backend: InterpreterBackend,
) -> None:
    found = await backend.records([Author.ref(handle="ana")])

    rebuilt = records_from_json(records_to_json(found))

    assert rebuilt == found


def test_an_unreadable_record_payload_is_refused() -> None:
    """A version this build cannot read fails here rather than as a missing field
    somewhere in a projection."""
    with pytest.raises(q.QueryWireError, match="version"):
        records_from_json({"v": 99, "records": []})


async def test_a_conflict_survives_the_crossing() -> None:
    """A conflict is an input the answer could not confirm (ADR-0014 D5).
    Dropping it on the wire would let a projection render a contested field as
    settled -- C9's silent last-write-wins, reintroduced by a serializer."""
    store = FactStore(LIBCAT_SCHEMA)
    ana = Author.ref(handle="ana")
    store.ingest(
        AUTHORS,
        [FieldFact(entity=ana, field="display_name", value="Ana Reyes", prov=_prov())],
    )
    store.ingest(
        AUTHORS,
        [
            FieldFact(
                entity=ana, field="display_name", value="A. Reyes", prov=_prov(line=9)
            )
        ],
        unit_id="contested",
    )
    backend = InterpreterBackend(store, schema=LIBCAT_SCHEMA)

    (record,) = records_from_json(records_to_json(await backend.records([ana])))

    assert len(record.conflicts) == 1
    assert {v.value for v in record.conflicts[0].values} == {"Ana Reyes", "A. Reyes"}


# ---- LiteralKind.KNOWN -------------------------------------------------


async def test_a_key_literal_alone_matches_an_entity_that_does_not_exist(
    backend: InterpreterBackend,
) -> None:
    """Not a defect -- ADR-0019 D1's design, and the premise for the next test.
    A KEY literal *constructs* the reference; it reads nothing."""
    author = q.var(Author)

    result = (
        await q.query(author).where(Author.key(author, handle="nobody")).all(backend)
    )

    assert len(result.rows) == 1


async def test_known_narrows_a_key_literal_to_entities_the_store_has_seen(
    backend: InterpreterBackend,
) -> None:
    """The existence question ADR-0013 D3 named and the query language could not
    ask. Its absence is why ``projection.py`` held a ``FactSource``."""
    author = q.var(Author)

    real = await (
        q.query(author)
        .where(Author.key(author, handle="ana"), Author.known(author))
        .all(backend)
    )
    fake = await (
        q.query(author)
        .where(Author.key(author, handle="nobody"), Author.known(author))
        .all(backend)
    )

    assert [row[0] for row in real.rows] == [Author.ref(handle="ana")]
    assert fake.rows == []


async def test_known_records_a_footprint_key_even_when_the_entity_is_absent(
    backend: InterpreterBackend,
) -> None:
    """The empty case is the one that matters: "this action is unknown" rests on
    the absence of facts, so a first fact about it must invalidate the answer.
    Recording only on a hit is D4.3's silent false negative in miniature."""
    author = q.var(Author)

    await (
        q.query(author)
        .where(Author.key(author, handle="nobody"), Author.known(author))
        .all(backend)
    )

    assert fp.entity_key(Author.ref(handle="nobody")) in backend.last_footprint


async def test_known_refuses_to_enumerate(backend: InterpreterBackend) -> None:
    """It filters; it cannot generate. Binding an entity from it would mean
    walking every reference the store has ever seen -- ADR-0019 D6's refusal."""
    author = q.var(Author)

    with pytest.raises(SchemaError, match="bound by an earlier literal"):
        await q.query(author).where(Author.known(author)).all(backend)


async def test_known_composes_with_a_join(backend: InterpreterBackend) -> None:
    """An entity bound by an edge or field literal is already known, so ``known``
    is redundant there and must not change the answer."""
    book, isbn = q.var(Book), q.var(str)

    with_known = await (
        q.query(book, isbn)
        .where(BookFields.isbn(book, isbn), Book.known(book))
        .all(backend)
    )
    without = await q.query(book, isbn).where(BookFields.isbn(book, isbn)).all(backend)

    assert with_known.rows == without.rows


async def test_a_negated_known_finds_merely_referenced_entities() -> None:
    """The complement, and a real question: an entity that appears as an edge
    endpoint but that nobody asserted anything about. ADR-0019 D3's warning is
    about exactly this set, and this is how a rule names it."""
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        AUTHORS,
        [
            FieldFact(
                entity=Author.ref(handle="ana"),
                field="handle",
                value="ana",
                prov=_prov(),
            )
        ],
    )
    backend = InterpreterBackend(store, schema=LIBCAT_SCHEMA)
    author = q.var(Author)

    result = await (
        q.query(author)
        .where(Author.key(author, handle="ghost"), q.not_(Author.known(author)))
        .all(backend)
    )

    assert [row[0] for row in result.rows] == [Author.ref(handle="ghost")]


async def test_a_known_literal_crosses_the_wire(backend: InterpreterBackend) -> None:
    """A new literal kind that did not serialize would work in-process and fail
    the moment the query crossed to the WM -- which is now the ordinary path."""
    author = q.var(Author)
    built = q.query(author).where(
        Author.key(author, handle="ana"), Author.known(author)
    )

    rebuilt = q.query_from_json(q.query_to_json(built), LIBCAT_SCHEMA)

    assert q.query_to_json(rebuilt) == q.query_to_json(built)
    assert (await rebuilt.all(backend)).rows == [(Author.ref(handle="ana"),)]
