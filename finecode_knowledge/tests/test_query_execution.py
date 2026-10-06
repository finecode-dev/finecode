"""The direct interpreter, the footprint, and the freshness verdict.

Two of these tests are the ones ADR-0013 names as never yet exercised. Both
guard a bug whose failure direction is the dangerous one -- not a crash, but a
rule that quietly stops reporting a real violation:

- `test_an_empty_scan_records_the_slot_that_would_make_it_non_empty` is D4.3's
  negation case, where the plausible implementation (record what you returned)
  is silently wrong.
- `test_a_contested_slot_reaches_the_verdict_through_a_value_bound_literal` is
  ADR-0014 D4's, where the same failure direction appears as a result claiming
  verification over an input two providers disagree about.

Written against ``libcat`` (``tests/fixtures/libcat``), the engine's own
synthetic schema, so that a leaked assumption about a specific vocabulary fails
here rather than coincidentally passing (R18/R19).

## What these tests do not yet establish, and when to revisit

**They assert a key-matching property, not end-to-end cache invalidation.** The
memo DAG (R1-R4) does not exist, so nothing here consumes a footprint and
decides to recompute. What is asserted is the half the query layer owns: the
footprint recorded by an empty scan *matches* the fact that would have made it
non-empty, under ADR-0013 D4.2's wildcard rule. The walk that acts on that match
is memo-DAG work.

**Revisit when the memo DAG lands**, and add the assertion these cannot make:
add the fact, re-run, and check the violation actually appears -- i.e. that the
match caused a recomputation rather than merely being available to one. Until
then a memo layer could record every key correctly and still never look at them,
and this suite would pass.

**A known hole these tests cannot see: `O8`** (`python-api-design.md` §8, open
2026-07-28). An access key names a fact *slot*, and `footprint.matches` compares
a `FieldFact` on `(entity.type, field, entity, value)` and an `EdgeFact` on
`(kind, src, dst)`. Provenance participates in neither, and `prov` is
`compare=False` on both fact types -- deliberately, so re-extraction without
change hashes equal and R5/C8's early cutoff can fire. So a declaration that
*moves within its file* without changing produces an identical fact matching no
recorded key: a memoized violation would keep a **stale line number**. Not a
wrong answer -- the violation still holds -- but a wrong location, which for a
diagnostic is most of the value. It must be settled before the memo layer serves
a cached violation; it blocks nothing today.
"""

from __future__ import annotations

import pytest
from libcat import (
    LIBCAT_SCHEMA,
    Author,
    AuthorFields,
    Book,
    BookFields,
    Rel,
    Shelf,
    ShelfFields,
)

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import (
    EdgeFact,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.verify import VerifyReport
from finecode_knowledge.query import footprint as fp
from finecode_knowledge.query.freshness import ReservationKind
from finecode_knowledge.query.interpret import InterpreterBackend

_CATALOG = "libcat.catalog_scan"
_SHELVES = "libcat.shelf_survey"
_AUTHORS = "libcat.author_index"


def _run() -> RunStamp:
    return RunStamp(id="r-1", observed_at="2026-07-30T10:00:00Z")


def _prov(provider: str, *, project: str | None = None, line: int = 1) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=_run(),
        location=SourceLoc(project=project, file="catalog.toml", line=line),
    )


@pytest.fixture
def store() -> FactStore:
    """A small but real catalogue: two authors, their books, and one shelf.

    ``ana`` wrote book ``a-1``, which sits on shelf ``s-A``; ``bo`` wrote ``b-1``,
    borrowed ``a-1`` and frequents ``s-A``.
    """
    fact_store = FactStore(LIBCAT_SCHEMA)
    ana, bo = Author.ref(handle="ana"), Author.ref(handle="bo")
    a_book, b_book = Book.ref(isbn="a-1"), Book.ref(isbn="b-1")
    shelf = Shelf.ref(code="s-A")
    prov = _prov(_CATALOG, project="ana")

    # `Author.handle` is `author_index`'s to supply, `homepage` is `catalog_scan`'s.
    # SUPPLIES-bounding is enforced at ingest (C4), so the split is not cosmetic.
    fact_store.ingest(
        _AUTHORS,
        [
            FieldFact(
                entity=ana,
                field="handle",
                value="ana",
                prov=_prov(_AUTHORS, project="ana"),
            ),
            FieldFact(
                entity=bo,
                field="handle",
                value="bo",
                prov=_prov(_AUTHORS, project="bo"),
            ),
        ],
    )
    fact_store.ingest(
        _CATALOG,
        [
            FieldFact(entity=ana, field="homepage", value="ana.example", prov=prov),
            FieldFact(entity=bo, field="homepage", value="bo.example", prov=prov),
            FieldFact(entity=a_book, field="isbn", value="a-1", prov=prov),
            FieldFact(entity=b_book, field="isbn", value="b-1", prov=prov),
            EdgeFact(kind="wrote", src=ana, dst=a_book, prov=prov),
            EdgeFact(kind="wrote", src=bo, dst=b_book, prov=prov),
            EdgeFact(kind="borrowed", src=bo, dst=a_book, prov=prov),
            EdgeFact(kind="frequents", src=bo, dst=shelf, prov=prov),
        ],
    )
    fact_store.ingest(
        _SHELVES,
        [
            FieldFact(entity=shelf, field="code", value="s-A", prov=_prov(_SHELVES)),
            FieldFact(
                entity=shelf, field="room", value="reading-room", prov=_prov(_SHELVES)
            ),
            EdgeFact(kind="shelved_on", src=a_book, dst=shelf, prov=_prov(_SHELVES)),
        ],
    )
    return fact_store


@pytest.fixture
def backend(store: FactStore) -> InterpreterBackend:
    return InterpreterBackend(store)


# ---- ADR-0006 / ADR-0016: a Query is inert -----------------------------


def test_a_query_does_not_execute_when_iterated() -> None:
    """``__iter__``/``__len__``/``__bool__`` name the missing terminal (NFR6). An
    un-awaited coroutine is then a second, independent way the mistake surfaces
    loudly."""
    author = q.var(Author)
    built = q.query(author).where(AuthorFields.handle(author, q.var(str)))

    with pytest.raises(q.QueryNotExecutedError, match=r"query\.all\(backend\)"):
        list(built)
    with pytest.raises(q.QueryNotExecutedError, match=r"query\.count\(backend\)"):
        len(built)
    with pytest.raises(q.QueryNotExecutedError, match=r"query\.exists\(backend\)"):
        bool(built)


async def test_a_terminal_returns_rows_and_a_verdict(
    backend: InterpreterBackend,
) -> None:
    author, handle = q.var(Author), q.var(str)

    result = (
        await q.query(handle).where(AuthorFields.handle(author, handle)).all(backend)
    )

    assert {row[0] for row in result.rows} == {"ana", "bo"}
    assert result.freshness.revision


# ---- the walk ----------------------------------------------------------


async def test_a_join_yields_one_row_per_matching_pair(
    backend: InterpreterBackend,
) -> None:
    """A join produces a row per matching pair by construction, which is what a
    hand-written nested loop over the same two relations was defending by hand."""
    author, book, shelf = q.var(Author), q.var(Book), q.var(Shelf)

    result = await (
        q.query(author, shelf)
        .where(Rel.wrote(author, book), Rel.shelved_on(book, shelf))
        .all(backend)
    )

    assert result.rows == [(Author.ref(handle="ana"), Shelf.ref(code="s-A"))]


async def test_a_constant_in_term_position_filters(backend: InterpreterBackend) -> None:
    book = q.var(Book)

    result = await q.query(book).where(BookFields.isbn(book, "a-1")).all(backend)

    assert result.rows == [(Book.ref(isbn="a-1"),)]


async def test_at_binds_the_facts_provenance(backend: InterpreterBackend) -> None:
    """FR5: provenance is an ordinary bindable term, so a rule reaches it without a
    second read per row."""
    author, shelf, at = q.var(Author), q.var(Shelf), q.Prov()

    result = await q.query(at).where(Rel.frequents(author, shelf, at=at)).all(backend)

    (row,) = result.rows
    assert isinstance(row[0], Provenance)
    assert row[0].provider == _CATALOG


async def test_set_semantics_dedup_rows_without_a_seen_set(
    backend: InterpreterBackend,
) -> None:
    """A hand-maintained ``seen`` set disappears into set semantics."""
    author = q.var(Author)

    result = await (
        q.query(author)
        .where(Rel.wrote(author, q.var(Book)), AuthorFields.handle(author, q.var(str)))
        .all(backend)
    )

    assert len(result.rows) == len(set(result.rows))


async def test_negation_is_a_membership_test(backend: InterpreterBackend) -> None:
    """``bo`` borrowed book ``a-1`` but ``ana`` borrowed nothing, so only ``ana``
    survives the negation.

    Both of the negated literal's terms are bound by earlier positive literals, as
    §5.4 requires -- an unbound one asks whether *some* binding fails to exist, which
    is not what the author wrote and is rejected at construction."""
    author, book = q.var(Author), q.var(Book)

    result = await (
        q.query(author)
        .where(
            Rel.wrote(author, q.var(Book)),
            BookFields.isbn(book, "a-1"),
            q.not_(Rel.borrowed(author, book)),
        )
        .all(backend)
    )

    assert result.rows == [(Author.ref(handle="ana"),)]


async def test_order_by_makes_row_order_deterministic(
    backend: InterpreterBackend,
) -> None:
    """Query results are sets, so NFR8 is met by an explicit request rather than by
    relying on scan order."""
    author, handle = q.var(Author), q.var(str)
    built = q.query(handle).where(AuthorFields.handle(author, handle))

    result = await built.order_by(handle).all(backend)

    assert [row[0] for row in result.rows] == ["ana", "bo"]


async def test_order_by_rejects_a_term_outside_the_projection() -> None:
    author, handle = q.var(Author), q.var(str)
    built = q.query(handle).where(AuthorFields.handle(author, handle))

    with pytest.raises(SchemaError, match="not in the projection"):
        built.order_by(author)


# ---- derived predicates execute ----------------------------------------


@q.derived
def owns_shelf(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
    """*author* wrote a book that sits on *shelf*."""
    book = q.var(Book)
    return q.all_(Rel.wrote(author, book), Rel.shelved_on(book, shelf))


@q.derived
def borrowed_isbn(author: q.Var[Author], isbn: q.Var[str]) -> q.Body:
    """*author* borrowed a book catalogued under *isbn*."""
    book = q.var(Book)
    return q.all_(Rel.borrowed(author, book), BookFields.isbn(book, isbn))


@q.derived
def points_at_shelf(src: q.Var[Author], shelf: q.Var[Shelf], at: q.Prov) -> q.Body:
    return q.all_(Rel.frequents(src, shelf, at=at))


@points_at_shelf.clause
def _(src: q.Var[Shelf], shelf: q.Var[Shelf], at: q.Prov) -> q.Body:
    return q.all_(Rel.adjacent_to(src, shelf, at=at))


for _predicate in (owns_shelf, borrowed_isbn, points_at_shelf):
    LIBCAT_SCHEMA.register_predicate(_predicate)


async def test_a_derived_predicate_expands_and_joins(
    backend: InterpreterBackend,
) -> None:
    author, shelf = q.var(Author), q.var(Shelf)

    result = await q.query(author, shelf).where(owns_shelf(author, shelf)).all(backend)

    assert result.rows == [(Author.ref(handle="ana"), Shelf.ref(code="s-A"))]


async def test_a_derived_predicates_private_variables_do_not_leak(
    backend: InterpreterBackend,
) -> None:
    """Two calls to one predicate in one body must not join through its internals."""
    a, b, shelf = q.var(Author), q.var(Author), q.var(Shelf)

    result = await (
        q.query(a, b).where(owns_shelf(a, shelf), owns_shelf(b, shelf)).all(backend)
    )

    assert result.rows == [(Author.ref(handle="ana"), Author.ref(handle="ana"))]


async def test_clauses_union(backend: InterpreterBackend) -> None:
    """A second clause is disjunction; ``bo`` reaches the shelf through ``frequents``
    while nothing reaches it through ``adjacent_to``."""
    src, shelf, at = q.var(Author), q.var(Shelf), q.Prov()

    result = await q.query(src).where(points_at_shelf(src, shelf, at=at)).all(backend)

    assert result.rows == [(Author.ref(handle="bo"),)]


async def test_negation_of_a_derived_predicate(backend: InterpreterBackend) -> None:
    """A rule negating a *named* predicate must be evaluated as a membership test over
    the expansion, not just over a base relation."""
    author, shelf, missing = q.var(Author), q.var(Shelf), q.var(str)

    result = await (
        q.query(author, missing)
        .where(
            Rel.frequents(author, shelf),
            ShelfFields.code(shelf, missing),
            q.not_(borrowed_isbn(author, missing)),
        )
        .all(backend)
    )

    # `bo` frequents shelf `s-A` and has borrowed no book catalogued under that name.
    assert result.rows == [(Author.ref(handle="bo"), "s-A")]


async def test_a_derived_call_is_spelled_exactly_like_a_base_one(
    backend: InterpreterBackend,
) -> None:
    """FR2, executing. The rule body cannot tell which of these is stored."""
    author, shelf = q.var(Author), q.var(Shelf)

    derived_rows = (
        await q.query(author).where(owns_shelf(author, shelf)).all(backend)
    ).rows
    base_rows = (
        await q.query(author).where(Rel.frequents(author, shelf)).all(backend)
    ).rows

    assert derived_rows == [(Author.ref(handle="ana"),)]
    assert base_rows == [(Author.ref(handle="bo"),)]


async def test_runaway_recursion_raises_rather_than_hangs(
    backend: InterpreterBackend,
) -> None:
    """FR10 makes recursion expressible; evaluating a fixpoint is not built (§5.7)."""

    @q.derived
    def _loops(a: q.Var[Author], b: q.Var[Author]) -> q.Body:
        return q.all_(Rel.wrote(a, q.var(Book)), AuthorFields.handle(b, q.var(str)))

    LIBCAT_SCHEMA.register_predicate(_loops)
    # Rebind the clause to call itself, which no decorator spelling allows directly.
    recursive = _loops.predicate.clauses[0]
    object.__setattr__(recursive, "body", q.all_(_loops(*recursive.head)))

    author = q.var(Author)
    with pytest.raises(SchemaError, match="expansion exceeded"):
        await q.query(author).where(_loops(author, q.var(Author))).all(backend)


# ---- ADR-0013 D4: the footprint ---------------------------------------


async def test_an_empty_scan_records_the_slot_that_would_make_it_non_empty(
    store: FactStore, backend: InterpreterBackend
) -> None:
    """ADR-0013 D4.3 -- the one place a plausible implementation is silently wrong.

    A negation succeeds precisely when a scan comes back **empty**. If the
    footprint were the rows returned, an empty scan would record nothing, a fact
    added later would match no key, and the cached "no violation" would never be
    invalidated: a silent false negative in a system whose entire purpose is to
    report violations.

    The memo DAG does not exist, so this is not an end-to-end cache-invalidation
    test. What is asserted is the key-matching property the query layer owns: the
    footprint recorded by an empty scan matches the fact that would have made it
    non-empty, under D4.2's wildcard rule.
    """
    ana, b_book = Author.ref(handle="ana"), Book.ref(isbn="b-1")
    author, book = q.var(Author), q.var(Book)

    result = await (
        q.query(author)
        .where(
            AuthorFields.handle(author, "ana"),
            BookFields.isbn(book, "b-1"),
            q.not_(Rel.borrowed(author, book)),
        )
        .all(backend)
    )
    assert result.rows == [(ana,)], "ana has not borrowed book b-1 -- yet"

    would_break_it = EdgeFact(
        kind=Rel.borrowed.qualified_name,
        src=ana,
        dst=b_book,
        prov=_prov(_CATALOG),
    )
    invalidated = backend.last_footprint.invalidated_by(would_break_it)

    assert invalidated, (
        "the empty scan recorded no key that the new fact matches, so a memo layer "
        "would never recompute this rule -- D4.3's silent false negative"
    )
    assert fp.edge_key(Rel.borrowed.qualified_name, ana, b_book) in invalidated


async def test_an_abandoned_scan_records_the_same_key_as_a_drained_one(
    store: FactStore,
) -> None:
    """Early termination is safe because the key is recorded per *access*: a scan
    stopped after one element consulted the same slot as one drained fully."""
    author, handle = q.var(Author), q.var(str)
    built = q.query(author).where(AuthorFields.handle(author, handle))

    drained = InterpreterBackend(store)
    await built.all(drained)
    abandoned = InterpreterBackend(store)
    await built.exists(abandoned)

    assert set(drained.last_footprint.keys) == set(abandoned.last_footprint.keys)


async def test_a_footprint_key_is_recorded_per_access_not_per_row(
    backend: InterpreterBackend,
) -> None:
    """One unbound scan over two authors records one key, not two."""
    author, handle = q.var(Author), q.var(str)

    result = (
        await q.query(handle).where(AuthorFields.handle(author, handle)).all(backend)
    )

    assert len(result.rows) == 2
    scan_keys = [k for k in backend.last_footprint.keys if k[0] == "field"]
    assert scan_keys == [
        fp.field_key(
            Author.qualified_name(), AuthorFields.handle.qualified_name, None, None
        )
    ]


# ---- criterion 8 -------------------------------------------------------


class _CountingBackend:
    """Counts crossings -- the thing §7 criterion 8 measures.

    "Instrument the backend and assert a *constant* number of recorded reads per
    execution regardless of how many rows come back."
    """

    def __init__(self, inner: InterpreterBackend) -> None:
        self._inner = inner
        self.crossings = 0

    async def run(self, query, *, mode, limit=None):  # type: ignore[no-untyped-def]
        self.crossings += 1
        return await self._inner.run(query, mode=mode, limit=limit)


async def test_read_count_is_independent_of_result_size(store: FactStore) -> None:
    """Criterion 8. What must never happen is a read *per traversal* -- the observable
    form of R13b, which fails loudly if any read escapes the ``Query`` (R21)."""
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    built = q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )

    small = _CountingBackend(InterpreterBackend(store))
    two_rows = await built.all(small)

    for extra in range(20):
        author_ref = Author.ref(handle=f"a{extra}")
        book_ref = Book.ref(isbn=f"i{extra}")
        prov = _prov(_CATALOG)
        store.ingest(
            _CATALOG,
            [
                FieldFact(entity=book_ref, field="isbn", value=f"i{extra}", prov=prov),
                EdgeFact(kind="wrote", src=author_ref, dst=book_ref, prov=prov),
            ],
            unit_id=f"u{extra}",
        )
    large = _CountingBackend(InterpreterBackend(store))
    many_rows = await built.all(large)

    assert len(two_rows.rows) == 2
    assert len(many_rows.rows) == 22
    assert small.crossings == large.crossings == 1


# ---- ADR-0014: the verdict --------------------------------------------


async def test_the_file_loaded_path_carries_exactly_one_untracked_reservation(
    backend: InterpreterBackend,
) -> None:
    """ADR-0014 D7, the record's honesty clause. The fact-file digest identifies the
    facts served and says nothing about whether the sources have moved since, so a
    verdict of ``verified`` on this path would be wrong in the silent direction."""
    author = q.var(Author)

    result = (
        await q.query(author)
        .where(AuthorFields.handle(author, q.var(str)))
        .all(backend)
    )

    untracked = [
        r for r in result.freshness.reservations if r.kind is ReservationKind.UNTRACKED
    ]
    assert len(untracked) == 1
    assert not result.freshness.verified


@pytest.mark.parametrize("mode", [q.Mode.VERIFIED, q.Mode.CACHED])
async def test_an_execution_never_yields_a_cached_reservation(
    backend: InterpreterBackend, mode: q.Mode
) -> None:
    """ADR-0014 D6, from the interpreter's side, and **both** modes rather than
    only the verified one.

    ``CACHED`` means "this answer was not recomputed", so the only layer that can
    assert it is the layer that declined to recompute -- ``memo/walk.py``. An
    execution just did the work, whichever mode asked for it, so its answer is a
    verified one either way. Phase 5 made that structural rather than a
    consequence of there being no memo yet, and parametrizing is what says so: a
    verdict branching on the mode here would fail the ``CACHED`` case.

    It also pins the standalone, no-WM path (criterion 10, PRD-0007): with no
    memo in the process, a cached-mode read is simply a verified one.
    """
    author = q.var(Author)

    result = await (
        q.query(author)
        .where(AuthorFields.handle(author, q.var(str)))
        .all(backend, mode=mode)
    )

    assert all(
        r.kind is not ReservationKind.CACHED for r in result.freshness.reservations
    )


async def test_a_verified_store_with_every_bucket_confirmed_reports_verified(
    store: FactStore,
) -> None:
    """The reservation is a statement about the *inputs*, not decoration: when the
    cold-start walk ran and left nothing unconfirmed, the list is empty and
    ``verified`` follows.

    This replaced an ``extraction_tracked=True`` flag. R11 answers that question per
    bucket rather than per store, so the input is the walk's report -- and passing
    ``None`` (no walk ran) is what now falls back to ADR-0014 D7's standing
    reservation, rather than a caller being able to assert freshness it does not
    have."""
    tracked = InterpreterBackend(store, verdicts=VerifyReport(verdicts={}))
    author = q.var(Author)

    result = (
        await q.query(author)
        .where(AuthorFields.handle(author, q.var(str)))
        .all(tracked)
    )

    assert result.freshness.verified


async def test_a_contested_slot_reaches_the_verdict_through_a_value_bound_literal(
    store: FactStore,
) -> None:
    """ADR-0014 D4's case, and why detection lives at ingest.

    A value-bound literal scans only facts whose value matches, so a provider
    asserting a *different* value for the same slot is not in the scan.
    Interpreter-side detection would miss it and the result would claim
    ``verified`` over a contested input -- the same silent-wrongness direction
    D4.3 rejects for negation.
    """
    shelf = Shelf.ref(code="s-A")
    store.ingest(
        _SHELVES,
        [
            FieldFact(
                entity=shelf,
                field="room",
                value="OTHER-room",
                prov=_prov(_SHELVES, line=9),
            )
        ],
        unit_id="contested",
    )
    backend = InterpreterBackend(store)
    target = q.var(Shelf)

    result = (
        await q.query(target)
        .where(ShelfFields.room(target, "reading-room"))
        .all(backend)
    )

    contested = [
        r for r in result.freshness.reservations if r.kind is ReservationKind.CONTESTED
    ]
    assert len(contested) == 1, (
        "a value-bound literal must still surface the disagreement"
    )
    assert "reading-room" in contested[0].detail
    assert "OTHER-room" in contested[0].detail
    assert _SHELVES in contested[0].detail


async def test_a_contested_slot_is_reported_once_not_once_per_row(
    store: FactStore,
) -> None:
    """ADR-0014 D4: one reservation per slot, not per row."""
    shelf = Shelf.ref(code="s-A")
    store.ingest(
        _SHELVES,
        [
            FieldFact(
                entity=shelf,
                field="room",
                value="OTHER-room",
                prov=_prov(_SHELVES, line=9),
            )
        ],
        unit_id="contested",
    )
    backend = InterpreterBackend(store)
    target, room = q.var(Shelf), q.var(str)

    result = (
        await q.query(target, room).where(ShelfFields.room(target, room)).all(backend)
    )

    assert len(result.rows) == 2, "both values bind -- the interpreter does not pick"
    contested = [
        r for r in result.freshness.reservations if r.kind is ReservationKind.CONTESTED
    ]
    assert len(contested) == 1


async def test_the_contested_consultation_records_a_value_unbound_key(
    store: FactStore,
) -> None:
    """The key recorded for a contest consultation is value-*unbound*, so a conflicting
    fact arriving later matches it under D4.2's wildcard rule. A value-bound key would
    not -- which is precisely the hole that made interpreter-side detection unsound."""
    backend = InterpreterBackend(store)
    target = q.var(Shelf)

    await q.query(target).where(ShelfFields.room(target, "reading-room")).all(backend)

    assert (
        fp.field_key(
            Shelf.qualified_name(), ShelfFields.room.qualified_name, None, None
        )
        in backend.last_footprint
    )
