"""Attribution: which buckets a query's answer could have depended on (R11).

The load-bearing test here is `test_an_empty_scan_still_reserves_against_a_stale
_bucket`. A rule that succeeds on an empty scan -- every `q.not_(...)` -- depends
on the buckets that *might* have filled it, and attributing by the rows returned
reports a clean verdict over a stale input. Same failure direction as ADR-0013
D4.3, one layer up: not a crash, but a rule that quietly stops reporting a real
violation while claiming its answer was verified.

Written against ``libcat`` (``tests/fixtures/libcat``), so what is exercised is
the mapping from a footprint key to *some* schema's providers rather than to
FineCode's four (R18/R19).
"""

from __future__ import annotations

import pathlib

from libcat import (
    LIBCAT_SCHEMA,
    Author,
    AuthorFields,
    Book,
    BookFields,
    Rel,
    Shelf,
)

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.model.verify import verify_inputs
from finecode_knowledge.query import footprint as fp
from finecode_knowledge.query.attribution import providers_for_key
from finecode_knowledge.query.freshness import ReservationKind
from finecode_knowledge.query.interpret import InterpreterBackend

CATALOG = "libcat.catalog_scan"
AUTHORS = "libcat.author_index"


def _prov(provider: str) -> Provenance:
    return Provenance(
        band=Band.DECLARED, provider=provider, run=RunStamp(id="r", observed_at="t")
    )


# ---- the static mapping ------------------------------------------------


def test_a_field_key_attributes_to_the_providers_declaring_that_field():
    key = fp.field_key(
        Book.qualified_name(), BookFields.isbn.qualified_name, None, None
    )

    assert providers_for_key(key, LIBCAT_SCHEMA) == {CATALOG}


def test_a_field_key_for_a_single_supplier_field_does_not_reach_the_other_providers():
    """``Author.handle`` is ``author_index``'s alone; ``Author.homepage`` is
    ``catalog_scan``'s. The split is what makes isolation possible at all."""
    handle = fp.field_key(Author.qualified_name(), AuthorFields.handle.qualified_name)
    homepage = fp.field_key(
        Author.qualified_name(), AuthorFields.homepage.qualified_name
    )

    assert providers_for_key(handle, LIBCAT_SCHEMA) == {AUTHORS}
    assert providers_for_key(homepage, LIBCAT_SCHEMA) == {CATALOG}


def test_an_edge_key_attributes_by_supplies_edges():
    assert providers_for_key(fp.edge_key(Rel.cites.qualified_name), LIBCAT_SCHEMA) == {
        AUTHORS
    }
    assert providers_for_key(
        fp.edge_key(Rel.borrowed.qualified_name), LIBCAT_SCHEMA
    ) == {CATALOG}


def test_a_type_key_attributes_to_every_supplier_of_any_field_of_that_type():
    """``entities_of_type`` asks whether an entity exists, and an entity exists exactly
    when some provider asserted some field about it -- so narrowing to the fields the
    query went on to read would be unsound.

    Asserted as a property rather than a fixed set, because the set legitimately
    grows: ``tests/fixtures/annex_ext`` registers ``Author.lending_ban`` from
    *outside* ``libcat``, so when that fixture is loaded ``annex_ext.annex_audit``
    appears here too. That is the mechanism working, not pollution -- a third-party
    provider's stale bucket must reserve against a query over an entity type it
    contributes to, exactly like a first-party one.
    """
    both = providers_for_key(fp.type_key(Author.qualified_name()), LIBCAT_SCHEMA)

    assert {AUTHORS, CATALOG} <= both
    assert "libcat.shelf_survey" not in both
    assert "libcat.copy_census" not in both
    for provider_id in both:
        provider = LIBCAT_SCHEMA.provider(provider_id)
        assert any(
            f.entity == Author.qualified_name() for f in provider.SUPPLIES_FIELDS
        ), f"{provider_id} supplies no Author field and should not be attributed"


def test_a_third_party_providers_field_attributes_to_that_provider():
    """R18/R19 reaching the freshness layer: a package declaring a field on an entity
    it does not own is attributed like any other supplier."""
    # Imported as top-level `annex_ext`, never `tests.fixtures.annex_ext`: the
    # declaring package is the top level of `__module__` (ADR-0017 D3), so the
    # latter spelling would register under `tests` and this test would silently
    # be about nothing. `tests/conftest.py` puts `tests/fixtures` on sys.path
    # for exactly this reason.
    from annex_ext.rules import AnnexAuthorFields

    key = fp.field_key(
        Author.qualified_name(), AnnexAuthorFields.lending_ban.qualified_name
    )

    assert providers_for_key(key, LIBCAT_SCHEMA) == {"annex_ext.annex_audit"}


# ---- the reservations it produces --------------------------------------


def _store_with(tmp_path: pathlib.Path) -> tuple[FactStore, pathlib.Path]:
    (tmp_path / "a.toml").write_text("one")
    (tmp_path / "b.toml").write_text("two")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        CATALOG,
        [
            FieldFact(
                entity=Book.ref(isbn="a-1"),
                field=BookFields.isbn.id,
                value="a-1",
                prov=_prov(CATALOG),
            )
        ],
        unit=Unit(CATALOG, "a.toml", inputs=("a.toml",)).captured(tmp_path),
    )
    store.ingest(
        AUTHORS,
        [
            FieldFact(
                entity=Author.ref(handle="ana"),
                field=AuthorFields.handle.id,
                value="ana",
                prov=_prov(AUTHORS),
            )
        ],
        unit=Unit(AUTHORS, "b.toml", inputs=("b.toml",)).captured(tmp_path),
    )
    return store, tmp_path


async def test_a_clean_tree_yields_no_reservations(tmp_path):
    """Criterion 1. Before R11 every result carried one reservation unconditionally,
    so `verified` never meant anything on the default path."""
    store, root = _store_with(tmp_path)
    backend = InterpreterBackend(store, verdicts=verify_inputs(store, root))
    book = q.var(Book)

    result = await q.query(book).where(BookFields.isbn(book, q.var(str))).all(backend)

    assert result.freshness.reservations == ()
    assert result.freshness.verified


async def test_a_stale_bucket_reserves_only_against_queries_that_could_read_it(
    tmp_path,
):
    """Criterion 4. A rule reading only ``catalog_scan`` fields is unaffected by a stale
    ``author_index`` unit, and vice versa."""
    store, root = _store_with(tmp_path)
    (root / "b.toml").write_text("changed")
    backend = InterpreterBackend(store, verdicts=verify_inputs(store, root))

    book = q.var(Book)
    unaffected = (
        await q.query(book).where(BookFields.isbn(book, q.var(str))).all(backend)
    )
    author = q.var(Author)
    affected = (
        await q.query(author)
        .where(AuthorFields.handle(author, q.var(str)))
        .all(backend)
    )

    assert unaffected.freshness.reservations == ()
    assert [r.kind for r in affected.freshness.reservations] == [ReservationKind.STALE]
    assert affected.freshness.reservations[0].subject == f"{AUTHORS}/b.toml"


async def test_an_empty_scan_still_reserves_against_a_stale_bucket(tmp_path):
    """**The one that matters.** A scan matching nothing depends on the bucket that
    might have filled it. Attributing by rows returned would report this clean -- and
    the rule's "no violation" would rest on a unit whose source has since changed."""
    store, root = _store_with(tmp_path)
    (root / "b.toml").write_text("changed")
    backend = InterpreterBackend(store, verdicts=verify_inputs(store, root))
    author = q.var(Author)

    result = await (
        q.query(author)
        .where(AuthorFields.handle(author, "no-such-author"))
        .all(backend)
    )

    assert result.rows == [], "the premise: the scan returned nothing"
    assert [r.kind for r in result.freshness.reservations] == [ReservationKind.STALE]


async def test_a_deleted_input_reserves_as_stale_carrying_the_distinction_in_detail(
    tmp_path,
):
    """ADR-0022 D3: `MISSING` is a bucket verdict, not a reservation kind -- no caller
    acts differently on it, so it maps to STALE and says so in `detail`."""
    store, root = _store_with(tmp_path)
    (root / "b.toml").unlink()
    backend = InterpreterBackend(store, verdicts=verify_inputs(store, root))
    author = q.var(Author)

    result = (
        await q.query(author)
        .where(AuthorFields.handle(author, q.var(str)))
        .all(backend)
    )

    (reservation,) = result.freshness.reservations
    assert reservation.kind is ReservationKind.STALE
    assert "no longer exist" in reservation.detail


async def test_no_verification_falls_back_to_the_standing_reservation(tmp_path):
    """ADR-0014 D7 still governs a store nobody checked: `None` is not "fine"."""
    store, _ = _store_with(tmp_path)
    backend = InterpreterBackend(store)
    book = q.var(Book)

    result = await q.query(book).where(BookFields.isbn(book, q.var(str))).all(backend)

    assert [r.kind for r in result.freshness.reservations] == [
        ReservationKind.UNTRACKED
    ]


def test_merge_deduplicates_reservations_naming_the_same_input():
    """ADR-0025 D5. Three rules reading one stale unit share one reservation; a
    multi-hop walk over twenty units must not hand an assistant twenty."""
    from finecode_knowledge.query.freshness import Freshness, Revision, unit_reservation

    one = unit_reservation(ReservationKind.STALE, "a.toml", CATALOG, "changed")
    same = unit_reservation(ReservationKind.STALE, "a.toml", CATALOG, "changed")
    other = unit_reservation(
        ReservationKind.UNTRACKED, "b.toml", AUTHORS, "no fingerprints"
    )
    revision = Revision("rev")

    merged = Freshness.merge(
        revision,
        Freshness(revision, (one,)),
        Freshness(revision, (same, other)),
        Freshness(revision, (one,)),
    )

    assert merged.reservations == (one, other), "deduplicated, first-seen order kept"
    assert not merged.verified


def test_a_shelf_key_never_attributes_to_a_book_supplier():
    """Sanity on the discrimination itself: an unrelated entity type's key must not
    pick up providers that supply nothing of it."""
    assert providers_for_key(fp.type_key(Shelf.qualified_name()), LIBCAT_SCHEMA) == {
        "libcat.shelf_survey"
    }
