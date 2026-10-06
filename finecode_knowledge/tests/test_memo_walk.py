"""The verification walk, invalidation and early cutoff (Phases 3-4).

**Everything here asserts on node visits and recompute counts, never on wall
time.** That is not a stylistic preference: a node that cut off and a node that
recomputed to an identical value return the same rows, so the answer cannot tell
them apart, and a timing assertion measures the machine. Criterion 1 (O(1) for an
unaffected query), criterion 3 (a change computes nothing) and criterion 4
(identical facts do not propagate) are all statements about ``WalkStats``.

The acceptance criteria are named on the tests that carry them.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.memo.keys import extraction_key, query_key
from finecode_knowledge.memo.node import FIRST_REVISION, Materialize, NodeKind
from finecode_knowledge.memo.table import MemoTable
from finecode_knowledge.memo.walk import MemoWalk, WalkStats
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import (
    EdgeFact,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.freshness import ReservationKind
from finecode_knowledge.query.interpret import InterpreterBackend

CATALOG = "libcat.catalog_scan"
SHELVES = "libcat.shelf_survey"
CATALOG_UNIT = (CATALOG, "catalog.toml")
SHELF_UNIT = (SHELVES, "shelves.toml")


def _prov(provider: str, line: int = 1) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=RunStamp(id="r", observed_at="t"),
        location=SourceLoc(project="p", file="catalog.toml", line=line),
    )


def _catalog_facts(*, line: int = 7, extra_book: bool = False) -> list:
    facts = [
        FieldFact(
            entity=Book.ref(isbn="a-1"), field="isbn", value="a-1", prov=_prov(CATALOG)
        ),
        EdgeFact(
            kind="wrote",
            src=Author.ref(handle="ana"),
            dst=Book.ref(isbn="a-1"),
            prov=_prov(CATALOG, line),
        ),
    ]
    if extra_book:
        facts.append(
            FieldFact(
                entity=Book.ref(isbn="b-9"),
                field="isbn",
                value="b-9",
                prov=_prov(CATALOG),
            )
        )
    return facts


class _Harness:
    """A store, a table and a walk, plus a count of how often the interpreter ran.

    ``executions`` is separate from ``WalkStats.recomputes`` on purpose: the first
    counts what actually touched a fact, the second counts what the walk *decided*.
    Asserting both is how criterion 3 -- "a change event computes nothing" -- is
    checked from the outside as well as the inside.
    """

    def __init__(self, refresh=None) -> None:
        self.store = FactStore(LIBCAT_SCHEMA)
        self.table = MemoTable()
        self.executions = 0
        self.ingest_catalog()
        self.ingest_shelves()
        self.walk = MemoWalk(
            self.table, self.store, LIBCAT_SCHEMA, self._execute, refresh=refresh
        )

    async def _execute(self, built, mode, limit):
        self.executions += 1
        backend = InterpreterBackend(self.store, schema=LIBCAT_SCHEMA)
        result = await backend.run(built, mode=mode, limit=limit)
        return result, backend.last_footprint.keys

    def ingest_catalog(self, **kwargs) -> None:
        self.store.ingest(
            CATALOG, _catalog_facts(**kwargs), unit=Unit(*CATALOG_UNIT, inputs=())
        )

    def ingest_shelves(self) -> None:
        self.store.ingest(
            SHELVES,
            [
                FieldFact(
                    entity=Shelf.ref(code="s-A"),
                    field="code",
                    value="s-A",
                    prov=_prov(SHELVES),
                )
            ],
            unit=Unit(*SHELF_UNIT, inputs=()),
        )

    async def ask(self, built, **kwargs) -> tuple[WalkStats, object]:
        self.walk.stats.reset()
        before = self.executions
        result = await self.walk.answer(built, **kwargs)
        stats = dataclasses.replace(self.walk.stats)
        self.executed = self.executions - before
        return stats, result


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


def _books_by_author() -> q.Query:
    """Insensitive: no ``Prov`` reaches the projection."""
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    return q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )


def _where_written() -> q.Query:
    """Sensitive: the projection carries a ``Prov`` (ADR-0027 D3)."""
    author, book, at = q.var(Author), q.var(Book), q.Prov()
    return q.query(author, at).where(Rel.wrote(author, book, at=at))


def _all_isbns() -> q.Query:
    """Unjoined, so ``extra_book=True`` actually moves the answer -- ``wrote``
    reaches only the first book."""
    book, isbn = q.var(Book), q.var(str)
    return q.query(book, isbn).where(BookFields.isbn(book, isbn))


def _shelf_codes() -> q.Query:
    """Reads only ``shelf_survey``, so a catalogue change cannot affect it."""
    shelf, code = q.var(Shelf), q.var(str)
    from libcat import ShelfFields

    return q.query(shelf, code).where(ShelfFields.code(shelf, code))


# ---- step 1: the memo hit (criterion 1, R22) ---------------------------


async def test_a_first_query_computes(harness: _Harness) -> None:
    stats, result = await harness.ask(_books_by_author())

    assert stats.recomputes == 1
    assert harness.executed == 1
    assert result.rows == [(Author.ref(handle="ana"), "a-1")]


async def test_asking_again_at_the_same_revision_is_one_node_visit(
    harness: _Harness,
) -> None:
    """**Criterion 1** (R22), asserted on node visits rather than wall time.

    Step 1 returns without looking at a single dependency, which is the common
    case in an editor loop where most queries are unaffected by the edit."""
    built = _books_by_author()
    await harness.ask(built)

    stats, _ = await harness.ask(built)

    assert (stats.hits, stats.recomputes, stats.node_visits) == (1, 0, 1)
    assert harness.executed == 0


async def test_two_different_questions_do_not_share_a_node(harness: _Harness) -> None:
    await harness.ask(_books_by_author())

    stats, _ = await harness.ask(_shelf_codes())

    assert stats.recomputes == 1, "a different question is a different node"


# ---- invalidation computes nothing (criterion 3, R2) -------------------


async def test_a_change_event_computes_nothing(harness: _Harness) -> None:
    """**Criterion 3.** ``invalidate`` marks; it does not run a provider or a rule
    body. This is §4.1's split made observable: if invalidation computed, its cost
    would be the graph rather than the change, and every keystroke would pay it."""
    await harness.ask(_books_by_author())
    before_executions = harness.executions
    before_visits = harness.walk.stats.node_visits

    harness.table.invalidate([CATALOG_UNIT])

    assert harness.executions == before_executions, "no rule body ran"
    assert harness.table.dirty == {CATALOG_UNIT}
    assert harness.walk.stats.node_visits == before_visits, "and no node was walked"


async def test_invalidating_nothing_does_not_advance_the_revision(
    harness: _Harness,
) -> None:
    """A debounced batch that turned out to be empty must cost nothing. Advancing
    anyway would make every node fail step 1 for no reason."""
    before = harness.table.revision

    assert harness.table.invalidate([]) == before


async def test_the_revision_advances_once_per_change_not_once_per_unit(
    harness: _Harness,
) -> None:
    """§4.2: one monotonically increasing counter for the whole store. A per-unit
    counter would make "the revision this node was verified at" meaningless."""
    before = harness.table.revision

    after = harness.table.invalidate([CATALOG_UNIT, SHELF_UNIT])

    assert after == before + 1


# ---- step 3: verified without recomputing ------------------------------


async def test_an_unaffected_query_is_verified_without_recomputing(
    harness: _Harness,
) -> None:
    """**Criterion 2's shape.** A query reading only ``shelf_survey`` is untouched by
    a catalogue change: step 2 checks its dependency, step 3 sees nothing moved,
    and the memoized value is returned."""
    await harness.ask(_shelf_codes())
    harness.table.invalidate([CATALOG_UNIT])

    stats, _ = await harness.ask(_shelf_codes())

    assert (stats.verified_without_recompute, stats.recomputes) == (1, 0)
    assert harness.executed == 0


async def test_a_re_extraction_producing_identical_facts_does_not_recompute(
    harness: _Harness,
) -> None:
    """**Criterion 4**, and §4.4's whole reason for existing. The source changed --
    that is what put the bucket on the dirty list -- and the facts did not, so the
    change stops here instead of propagating to everything downstream."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog()
    harness.table.invalidate([CATALOG_UNIT])

    stats, result = await harness.ask(built)

    assert stats.digest_cutoffs == 1, "the bucket re-read to an identical multiset"
    assert (stats.verified_without_recompute, stats.recomputes) == (1, 0)
    assert result.rows == [(Author.ref(handle="ana"), "a-1")]


async def test_a_real_fact_change_does_recompute(harness: _Harness) -> None:
    """The other half: cutoff must not be a way of never noticing anything."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    stats, _ = await harness.ask(built)

    assert (stats.recomputes, stats.digest_cutoffs) == (1, 0)
    assert harness.executed == 1


async def test_a_recompute_producing_the_same_rows_does_not_advance_changed_at(
    harness: _Harness,
) -> None:
    """§4.3 step 4's second half, one layer above extraction. Adding a book nothing
    in this query matches forces a recomputation and produces the same answer -- so
    ``changed_at`` stays put, and anything reading *this* node cuts off in turn.

    Advancing ``changed_at`` unconditionally is the single change that would
    disable early cutoff everywhere while leaving every answer-checking test
    passing."""
    author, book = q.var(Author), q.var(Book)
    built = q.query(author).where(Rel.wrote(author, book))
    await harness.ask(built)
    node_key = harness.walk.provenance_of(built).node_key
    _, changed_before = harness.table.revisions_of(node_key)

    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])
    stats, _ = await harness.ask(built)

    verified_after, changed_after = harness.table.revisions_of(node_key)
    assert stats.recomputes == 1, "the bucket's facts changed, so it had to run"
    assert changed_after == changed_before, "but the answer did not, so it stops here"
    assert verified_after == harness.table.revision


# ---- ADR-0027: two cut strengths (criterion 5) -------------------------


async def test_a_line_move_recomputes_a_sensitive_node_and_updates_the_location(
    harness: _Harness,
) -> None:
    """**Criterion 5**, first half. The fact is identical -- ``prov`` is
    ``compare=False`` -- so the digest holds; a node whose value can contain a
    ``SourceLoc`` must recompute anyway, or it serves a stale line number."""
    built = _where_written()
    _, first = await harness.ask(built)
    assert first.rows[0][1].location.line == 7

    harness.ingest_catalog(line=99)
    harness.table.invalidate([CATALOG_UNIT])
    stats, second = await harness.ask(built)

    assert stats.recomputes == 1, "a sensitive node does not get the digest cut"
    assert second.rows[0][1].location.line == 99


async def test_the_same_edit_does_not_recompute_an_insensitive_node(
    harness: _Harness,
) -> None:
    """**Criterion 5**, second half, and the reason the classification is worth
    having: the strong cut survives for every node that cannot expose a location."""
    built = _books_by_author()
    await harness.ask(built)

    harness.ingest_catalog(line=99)
    harness.table.invalidate([CATALOG_UNIT])
    stats, _ = await harness.ask(built)

    assert (stats.recomputes, stats.digest_cutoffs) == (0, 1)
    assert stats.verified_without_recompute == 1


async def test_a_sensitive_node_still_cuts_off_when_nothing_was_invalidated(
    harness: _Harness,
) -> None:
    """Sensitivity costs recomputation only when an attributed bucket's inputs
    moved. Without a change it is an ordinary memo hit -- the classification is not
    "always recompute"."""
    built = _where_written()
    await harness.ask(built)

    stats, _ = await harness.ask(built)

    assert stats.hits == 1


# ---- R12 / D-6: one code path, two triggers ----------------------------


async def test_the_two_triggers_are_one_code_path(harness: _Harness) -> None:
    """**Criterion 6.** A watcher event and a cold-start fingerprint diff both
    arrive as "these units changed", and ``invalidate`` cannot tell which produced
    the list. R12 is then a property of there being no second path.

    Asserted by running the same edit through two harnesses whose only difference
    is how the unit list was obtained, and comparing the whole outcome."""
    watcher = _Harness()
    cold_start = _Harness()
    built = _books_by_author()
    await watcher.ask(built)
    await cold_start.ask(built)

    for harness_under_test, units in (
        (watcher, [CATALOG_UNIT]),
        # What a fingerprint diff would have produced: the same key, derived from
        # comparing what is on disk rather than from an event.
        (
            cold_start,
            [unit.key for unit in cold_start.store.units() if unit.key == CATALOG_UNIT],
        ),
    ):
        harness_under_test.ingest_catalog(extra_book=True)
        harness_under_test.table.invalidate(units)

    watcher_stats, watcher_result = await watcher.ask(built)
    cold_stats, cold_result = await cold_start.ask(built)

    assert watcher_result.rows == cold_result.rows
    assert watcher_stats == cold_stats
    assert watcher.table.revision == cold_start.table.revision


# ---- R15: a memo hit still unwinds -------------------------------------


async def test_a_memoized_answer_still_names_the_facts_it_rests_on(
    harness: _Harness,
) -> None:
    """**Criterion 8**, and the requirement easiest to lose in step 1 -- whose whole
    point is returning *without looking*. So the footprint and the attributed
    buckets stay on the node across hits rather than being reconstructed."""
    built = _books_by_author()
    await harness.ask(built)
    stats, _ = await harness.ask(built)
    assert stats.hits == 1, "the premise: this answer was served, not computed"

    provenance = harness.walk.provenance_of(built)

    assert provenance is not None
    assert CATALOG_UNIT in provenance.buckets
    assert SHELF_UNIT not in provenance.buckets, "it read no shelf facts"
    assert any(key[0] == "edge" for key in provenance.footprint)
    assert not provenance.location_sensitive


async def test_provenance_is_absent_before_anything_has_been_asked(
    harness: _Harness,
) -> None:
    assert harness.walk.provenance_of(_books_by_author()) is None


# ---- the table itself ---------------------------------------------------


async def test_the_walk_records_both_node_kinds(harness: _Harness) -> None:
    """Phase 2's "done when": executing a query populates nodes with footprints and
    keys."""
    await harness.ask(_books_by_author())

    queries = harness.table.of_kind(NodeKind.QUERY)
    extractions = harness.table.of_kind(NodeKind.EXTRACTION)

    assert len(queries) == 1
    assert queries[0].footprint and queries[0].depends_on
    assert [node.key for node in extractions] == [extraction_key(CATALOG_UNIT)]
    assert extractions[0].digest is not None


async def test_a_node_marked_recompute_is_never_served(harness: _Harness) -> None:
    """``MATERIALIZE``'s per-node policy (§4.13, D-5). It is not in the key and not
    in the version hash, so flipping it invalidates nothing -- it only stops the
    value being handed back."""
    built = _books_by_author()
    await harness.ask(built)
    node = harness.table.of_kind(NodeKind.QUERY)[0]
    node.materialize = Materialize.RECOMPUTE

    stats, _ = await harness.ask(built)

    assert (stats.hits, stats.recomputes) == (0, 1)


async def test_clearing_the_table_keeps_the_revision(harness: _Harness) -> None:
    """The revision is a statement about the world, not about the table. Rewinding
    it would let a node verified at the old number look current against the new."""
    harness.table.invalidate([CATALOG_UNIT])
    before = harness.table.revision

    harness.table.clear()

    assert harness.table.revision == before
    assert len(harness.table) == 0


def test_the_table_starts_after_the_never_revision() -> None:
    """A fresh node is born at ``INITIAL_REVISION`` and must not read as verified.
    With the counter starting there too, step 1 would serve a node that has never
    run -- returning a plausible empty answer and computing nothing, ever."""
    assert MemoTable().revision == FIRST_REVISION
    assert FIRST_REVISION != 0


# ---- verify-then-execute (ADR-0013 D6.3) -------------------------------


async def test_dependencies_are_verified_before_the_interpreter_runs(
    harness: _Harness,
) -> None:
    """The ordering ADR-0013 D6.3 asks for, asserted where it is observable: on a
    recompute the extraction node has already been visited when the interpreter is
    called, so the long awaiting work is sequenced ahead of the short synchronous
    walk rather than interleaved with it."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    seen_at_execute: list[int] = []
    original = harness.walk._execute

    async def spy(query, mode, limit):
        node = harness.table.get(extraction_key(CATALOG_UNIT))
        seen_at_execute.append(node.verified_at)
        return await original(query, mode, limit)

    harness.walk._execute = spy
    await harness.ask(built)

    assert seen_at_execute == [harness.table.revision], (
        "the dependency was verified at the current revision before execution began"
    )


async def test_a_query_with_a_limit_is_its_own_node(harness: _Harness) -> None:
    """Sharing a node between a full scan and an existence check would serve one row
    where every row was asked for."""
    built = _books_by_author()
    await harness.ask(built)

    stats, _ = await harness.ask(built, limit=1)

    assert stats.recomputes == 1


# ---- the read modes (criterion 9, ADR-0014 D6, §4.12) ------------------


def _cached(result) -> list:
    return [
        r for r in result.freshness.reservations if r.kind is ReservationKind.CACHED
    ]


async def test_a_cached_read_with_nothing_memoized_computes(harness: _Harness) -> None:
    """``Mode.CACHED`` is "serve what you have", and with an empty memo it has
    nothing. Returning an empty answer here would be the silent staleness G3
    forbids dressed up as a latency win, so it blocks and computes -- and the
    value it produces *was* verified, so it carries no reservation."""
    stats, result = await harness.ask(_books_by_author(), mode=Mode.CACHED)

    assert (stats.recomputes, stats.cached_serves) == (1, 0)
    assert result.rows == [(Author.ref(handle="ana"), "a-1")]
    assert not _cached(result)


async def test_a_cached_read_after_a_change_serves_the_memo_and_says_so(
    harness: _Harness,
) -> None:
    """**Criterion 9.** The value the last verified pass computed comes straight
    back, unverified against the new world, carrying the admission."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    stats, result = await harness.ask(built, mode=Mode.CACHED)

    assert (stats.cached_serves, stats.recomputes, stats.node_visits) == (1, 0, 1)
    assert harness.executed == 0, "no fact was read"
    assert result.rows == [(Author.ref(handle="ana"), "a-1")], "the pre-change answer"
    assert len(_cached(result)) == 1


async def test_a_cached_read_hits_the_node_a_verified_pass_filled(
    harness: _Harness,
) -> None:
    """The mode is not in the node key, so the two modes share one node. Were it
    keyed, a cached read could only ever hit what an earlier *cached* read
    computed -- a private second cache, and the latency §4.12 exists to remove."""
    built = _books_by_author()
    await harness.ask(built)  # verified
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    await harness.ask(built, mode=Mode.CACHED)

    assert len(harness.table.of_kind(NodeKind.QUERY)) == 1


async def test_a_cached_read_current_at_this_revision_reserves_nothing(
    harness: _Harness,
) -> None:
    """A cached read of a node the walk already verified at this revision is not
    stale in any sense. Reserving anyway would train every caller to ignore the
    kind, which is how a freshness verdict becomes decoration."""
    built = _books_by_author()
    await harness.ask(built)

    stats, result = await harness.ask(built, mode=Mode.CACHED)

    assert (stats.hits, stats.cached_serves) == (1, 0)
    assert not _cached(result)


async def test_a_cached_serve_does_not_verify_the_node(harness: _Harness) -> None:
    """A read is not a verification. If serving advanced ``verified_at``, the next
    *verified* query would take step 1 and return the stale value as current --
    the mode leaking its weaker contract into the stronger one."""
    built = _all_isbns()
    node_key = query_key(built, LIBCAT_SCHEMA, limit=None)
    await harness.ask(built)
    stale_at = harness.table.revisions_of(node_key)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    _, cached = await harness.ask(built, mode=Mode.CACHED)
    assert len(cached.rows) == 1, "the pre-change answer"
    assert harness.table.revisions_of(node_key) == stale_at

    stats, result = await harness.ask(built, mode=Mode.VERIFIED)

    assert stats.recomputes == 1
    assert len(result.rows) == 2, "the verified read sees the new book"


async def test_verified_mode_never_serves_a_cached_reservation(
    harness: _Harness,
) -> None:
    """ADR-0014 D6's other half: ``CACHED`` stays unreachable under
    ``Mode.VERIFIED``, which is what makes the mode contract a contract."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    _, result = await harness.ask(built, mode=Mode.VERIFIED)

    assert not _cached(result)


async def test_a_cached_serve_does_not_write_its_reservation_into_the_memo(
    harness: _Harness,
) -> None:
    """The reservation rides a copy. Written into the stored value it would leak
    twice over: the next *verified* read would hand back an answer reserved as
    cached, and every later cached serve would stack another reservation onto the
    same node."""
    built = _books_by_author()
    node_key = query_key(built, LIBCAT_SCHEMA, limit=None)
    await harness.ask(built)
    harness.table.invalidate([CATALOG_UNIT])

    for _ in range(3):
        _, served = await harness.ask(built, mode=Mode.CACHED)
        assert len(_cached(served)) == 1, "one serving-time reservation, not a pile"

    assert not _cached(harness.table.get(node_key).value)

    _, verified = await harness.ask(built, mode=Mode.VERIFIED)
    assert not _cached(verified)


# ---- cancel and restart (Phase 6.1, §4.11, R25, R13) -------------------


def _interpose(harness: _Harness, hook):
    """Run *hook* inside the walk's one await, where a change can land.

    Every cancellation test needs the same thing -- something to happen while the
    walk is suspended -- and the walk suspends in exactly one place, so this is
    where the whole hazard §4.11 describes is reachable at all.
    """
    original = harness.walk._execute

    async def wrapped(query, mode, limit):
        outcome = await original(query, mode, limit)
        await hook()
        return outcome

    harness.walk._execute = wrapped


async def test_a_change_landing_mid_walk_restarts_the_walk(harness: _Harness) -> None:
    """§4.11's adopted answer. The walk pinned a revision, the world moved out
    from under it, and rather than write a value verified against a world that no
    longer exists it starts again at the new one."""
    built = _all_isbns()
    landed = False

    async def land_a_change():
        nonlocal landed
        if landed:
            return
        landed = True
        harness.ingest_catalog(extra_book=True)
        harness.table.invalidate([CATALOG_UNIT])

    _interpose(harness, land_a_change)
    stats, result = await harness.ask(built)

    assert stats.restarts == 1
    assert stats.recomputes == 2, "the pass that was abandoned, and the one that stood"
    assert len(result.rows) == 2, (
        "the answer is the post-change one, not the pinned one"
    )


async def test_a_cancelled_pass_writes_nothing_to_its_node(harness: _Harness) -> None:
    """The discard is the point. A node that recorded the abandoned pass would
    claim a verification at a revision nothing checked -- and would then serve it
    to the next query through step 1, which is a stale answer with no reservation
    on it."""
    built = _all_isbns()
    node_key = query_key(built, LIBCAT_SCHEMA, limit=None)
    seen: list = []
    landed = False

    async def land_a_change():
        nonlocal landed
        seen.append(harness.table.revisions_of(node_key))
        if landed:
            return
        landed = True
        harness.ingest_catalog(extra_book=True)
        harness.table.invalidate([CATALOG_UNIT])

    _interpose(harness, land_a_change)
    await harness.ask(built)

    assert seen[1] == (0, 0), (
        "entering the second pass the node was still untouched: the first pass "
        "wrote nothing on its way out"
    )
    verified_at, _ = harness.table.revisions_of(node_key)
    assert verified_at == harness.table.revision
    assert len(harness.table.get(node_key).value.rows) == 2


async def test_a_restart_does_not_re_read_an_unaffected_bucket(
    harness: _Harness,
) -> None:
    """ "Reusing every still-valid memo entry" (§4.11), measured where reuse costs
    something: the restart re-reads the bucket the change marked and leaves the
    other alone. Falling back to a cold walk would make cancellation cost a full
    recomputation of everything, which is what MVCC was rejected for avoiding."""
    author, book, shelf, code, isbn = (
        q.var(Author),
        q.var(Book),
        q.var(Shelf),
        q.var(str),
        q.var(str),
    )
    from libcat import ShelfFields

    # Reads both buckets, so both are attributed dependencies.
    built = q.query(isbn, code).where(
        Rel.wrote(author, book),
        BookFields.isbn(book, isbn),
        ShelfFields.code(shelf, code),
    )
    await harness.ask(built)
    # The catalogue's facts have to actually move for the next walk to reach step
    # 4 at all -- an invalidation alone cuts off on the digest at step 3, and a
    # walk that never awaits has no in-flight window to interrupt.
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    read: list = []
    original_bucket = harness.store.bucket
    harness.store.bucket = lambda *key: (read.append(key), original_bucket(*key))[1]
    landed = False

    async def land_a_change():
        nonlocal landed
        if landed:
            return
        landed = True
        harness.table.invalidate([CATALOG_UNIT])

    _interpose(harness, land_a_change)
    stats, _ = await harness.ask(built)

    assert stats.restarts == 1
    assert SHELF_UNIT not in read, "nothing said the shelf survey moved"
    assert CATALOG_UNIT in read


async def test_restarts_are_bounded_by_the_number_of_changes(
    harness: _Harness,
) -> None:
    """**6.3.** The live-lock bound stated as the thing it actually is: each
    restart requires a revision advance of its own, so a walk terminates as soon
    as edits stop. Nothing caps the loop -- §4.11 bounds it upstream, at the
    watcher's debounce and at freshness being per-save rather than per-keystroke
    (§6) -- so what has to hold here is that the loop *tracks* changes exactly
    and never spins on its own."""
    built = _all_isbns()
    remaining = 3

    async def land_a_change():
        nonlocal remaining
        if remaining == 0:
            return
        remaining -= 1
        harness.table.invalidate([CATALOG_UNIT])

    _interpose(harness, land_a_change)
    stats, _ = await harness.ask(built)

    assert remaining == 0
    assert stats.restarts == 3, "one per change, and not one more"
    assert stats.recomputes == 4


# ---- the in-flight map (Phase 6.2, §4.11) ------------------------------


class _Gate:
    """Holds every execution open until released, so two can genuinely overlap.

    Concurrency here has to be *constructed*: the walk's only await resolves
    immediately against an in-memory store, so without a gate the first query
    would finish before the second was ever scheduled and the dedupe map would
    never be consulted.
    """

    def __init__(self) -> None:
        self.open = asyncio.Event()
        self.arrived = asyncio.Event()

    async def __call__(self) -> None:
        self.arrived.set()
        await self.open.wait()


async def test_two_concurrent_queries_for_one_node_share_one_execution(
    harness: _Harness,
) -> None:
    """§4.11's dedupe support. An LSP pass and an agent loop asking the same
    question at the same revision pay for one execution; without the map each
    launches its own, which is the load pattern the record names."""
    built = _all_isbns()
    gate = _Gate()
    _interpose(harness, gate)
    harness.walk.stats.reset()

    first = asyncio.create_task(harness.walk.answer(built))
    await gate.arrived.wait()
    second = asyncio.create_task(harness.walk.answer(built))
    await asyncio.sleep(0)
    gate.open.set()
    results = await asyncio.gather(first, second)

    assert harness.walk.stats.recomputes == 1
    assert harness.walk.stats.deduped == 1
    assert harness.executions == 1, "one execution reached the facts"
    assert results[0].rows == results[1].rows


async def test_two_different_questions_do_not_share_an_execution(
    harness: _Harness,
) -> None:
    """The map is keyed by node, not by "something is running". Coalescing two
    different questions would serve one's answer for the other."""
    gate = _Gate()
    _interpose(harness, gate)
    harness.walk.stats.reset()

    first = asyncio.create_task(harness.walk.answer(_all_isbns()))
    await gate.arrived.wait()
    second = asyncio.create_task(harness.walk.answer(_shelf_codes()))
    await asyncio.sleep(0)
    gate.open.set()
    isbns, codes = await asyncio.gather(first, second)

    assert harness.walk.stats.deduped == 0
    assert harness.walk.stats.recomputes == 2
    assert isbns.rows != codes.rows


async def test_a_failed_execution_leaves_nothing_in_flight(harness: _Harness) -> None:
    """The map is cleared in a ``finally``. A stuck entry would not be an error
    anyone could see -- it would be one question hanging forever, for the lifetime
    of the process, while every other question kept working."""
    built = _all_isbns()
    original = harness.walk._execute

    async def failing(query, mode, limit):
        raise RuntimeError("the store went away")

    harness.walk._execute = failing
    with pytest.raises(RuntimeError):
        await harness.walk.answer(built)

    assert harness.walk._inflight == {}

    harness.walk._execute = original
    result = await harness.walk.answer(built)
    assert result.rows == [(Book.ref(isbn="a-1"), "a-1")]


async def test_a_failed_execution_reaches_every_waiter(harness: _Harness) -> None:
    """A follower that swallowed the leader's failure would return no rows for a
    query that never ran -- the silent empty answer, arriving by a different
    route than the zero-revision bug but indistinguishable to a caller."""
    built = _all_isbns()
    gate = _Gate()

    async def failing(query, mode, limit):
        await gate()
        raise RuntimeError("the store went away")

    harness.walk._execute = failing

    first = asyncio.create_task(harness.walk.answer(built))
    await gate.arrived.wait()
    second = asyncio.create_task(harness.walk.answer(built))
    await asyncio.sleep(0)
    gate.open.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)


# ---- Phase 3: the pull edge (D-3, D-4, acceptance criteria 2 and 4) ----


def _install_refresh(harness: _Harness, fn) -> None:
    """Wire *fn* in as the walk's refresher, the same way every other test in
    this file swaps in a spy for ``_execute`` -- assigned after construction
    rather than threaded through the fixture, so a test only pays for what it
    actually exercises."""
    harness.walk._refresh = fn


async def test_a_dirty_bucket_is_refreshed_before_being_compared(
    harness: _Harness,
) -> None:
    """The mechanism itself: a refresher that actually changes the facts is
    awaited before the §4.4 comparison runs, so the comparison sees what the
    refresh left behind rather than what was there when the bucket went dirty."""
    built = _all_isbns()  # unjoined, so a new book actually moves the answer
    await harness.ask(built)
    calls: list = []

    async def refresh(bucket):
        calls.append(bucket)
        harness.ingest_catalog(extra_book=True)

    _install_refresh(harness, refresh)
    harness.table.invalidate([CATALOG_UNIT])

    stats, result = await harness.ask(built)

    assert calls == [CATALOG_UNIT]
    assert stats.refreshes == 1
    assert stats.recomputes == 1, "the refreshed facts are new, so the query reruns"
    assert len(result.rows) == 2


async def test_a_refresh_that_changes_nothing_still_cuts_off(
    harness: _Harness,
) -> None:
    """**Acceptance criterion 2, at the engine's own layer** -- and the whole
    point of the plan: the bucket is refreshed, the digest it re-reads to is
    identical, and nothing above the extraction node recomputes."""
    built = _books_by_author()
    await harness.ask(built)
    calls = 0

    async def refresh(bucket):
        nonlocal calls
        calls += 1
        harness.ingest_catalog()  # same content, re-ingested -- a no-op edit

    _install_refresh(harness, refresh)
    harness.table.invalidate([CATALOG_UNIT])

    stats, result = await harness.ask(built)

    assert calls == 1
    assert stats.refreshes == 1
    assert stats.digest_cutoffs == 1
    assert (stats.verified_without_recompute, stats.recomputes) == (1, 0)
    assert result.rows == [(Author.ref(handle="ana"), "a-1")]


async def test_a_refresh_does_not_itself_advance_the_revision(
    harness: _Harness,
) -> None:
    """D-4: the bucket is already on the dirty list, and consuming that mark is
    the refresh's whole job. Were the refresh itself to call ``invalidate``,
    the cancellation check right after its own await would fire on every
    refreshed bucket and force a restart per bucket -- a linear sweep turned
    quadratic."""
    built = _books_by_author()
    await harness.ask(built)

    async def refresh(bucket):
        harness.ingest_catalog(extra_book=True)  # mutates the store, not the table

    _install_refresh(harness, refresh)
    harness.table.invalidate([CATALOG_UNIT])
    pinned = harness.table.revision

    stats, _ = await harness.ask(built)

    assert stats.restarts == 0, "a well-behaved refresh must never trigger one"
    assert harness.table.revision == pinned


async def test_without_a_refresher_a_dirty_bucket_behaves_exactly_as_before(
    harness: _Harness,
) -> None:
    """D-3's regression bar, made its own assertion rather than left implicit in
    every pre-Phase-3 test above still passing unmodified: a walk built with
    ``refresh=None`` (the default) never calls the refresh path at all, on a
    dirty bucket or otherwise."""
    built = _books_by_author()
    await harness.ask(built)
    harness.ingest_catalog(extra_book=True)
    harness.table.invalidate([CATALOG_UNIT])

    stats, _ = await harness.ask(built)

    assert stats.refreshes == 0
    assert stats.recomputes == 1


async def test_a_change_landing_during_a_refresh_restarts_the_walk(
    harness: _Harness,
) -> None:
    """Step 3's cancellation check, at the point Phase 3 adds it: something
    unrelated changes while the refresh is doing its own work, and the walk
    discards the pass and restarts -- the same answer §4.11 already gives the
    query-execution await, applied to this one."""
    built = _all_isbns()  # unjoined, so a new book actually moves the answer
    await harness.ask(built)
    landed = False

    async def refresh(bucket):
        nonlocal landed
        harness.ingest_catalog(extra_book=True)
        if not landed:
            landed = True
            # A second, independent change lands while this refresh is still
            # doing its own work -- modelled synchronously since the harness's
            # refresh has no real await of its own to interleave at; what
            # matters is that it happens between the refresh call and the
            # walk's own revision check right after it.
            harness.table.invalidate([SHELF_UNIT])

    _install_refresh(harness, refresh)
    harness.table.invalidate([CATALOG_UNIT])

    stats, result = await harness.ask(built)

    assert stats.restarts == 1
    assert len(result.rows) == 2


async def test_two_walks_refreshing_the_same_bucket_share_one_call(
    harness: _Harness,
) -> None:
    """**Acceptance criterion 4.** Two concurrent walks needing the same dirty
    bucket (modelling two concurrent ``audit_code`` runs) join one refresh
    rather than each dispatching its own ER round trip -- the same in-flight
    join §4.11 already gives concurrent consumers of one query execution,
    applied at the bucket's own granularity (``_refresh_inflight``)."""
    gate = _Gate()
    calls = 0

    async def refresh(bucket):
        nonlocal calls
        calls += 1
        await gate()
        harness.ingest_catalog(extra_book=True)

    _install_refresh(harness, refresh)
    harness.table.invalidate([CATALOG_UNIT])
    built = _books_by_author()

    first = asyncio.create_task(harness.walk.answer(built))
    await gate.arrived.wait()
    second = asyncio.create_task(harness.walk.answer(built))
    await asyncio.sleep(0)
    gate.open.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert harness.walk.stats.refreshes == 1
    assert harness.walk.stats.deduped >= 1
    assert first_result.rows == second_result.rows


async def test_a_failed_refresh_reaches_every_waiter(harness: _Harness) -> None:
    """Mirrors ``test_a_failed_execution_reaches_every_waiter`` at the refresh's
    own dedupe map: a follower that swallowed the leader's failure would look
    like the refresh happened when it did not."""
    gate = _Gate()

    async def failing(bucket):
        await gate()
        raise RuntimeError("the runner went away")

    _install_refresh(harness, failing)
    harness.table.invalidate([CATALOG_UNIT])
    built = _books_by_author()

    first = asyncio.create_task(harness.walk.answer(built))
    await gate.arrived.wait()
    second = asyncio.create_task(harness.walk.answer(built))
    await asyncio.sleep(0)
    gate.open.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
    assert harness.walk._refresh_inflight == {}
