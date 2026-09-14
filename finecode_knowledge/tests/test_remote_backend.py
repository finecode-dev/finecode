"""Shipping the query instead of interpreting it (``goals.md`` §4.10, R13b).

The two properties worth pinning are **substitutability** -- a rule cannot tell
which backend it has, which is why ADR-0016 made terminals async -- and
**granularity**: one message per execution, independent of how many facts
answering it touched. The second is the one that quietly rots, because a backend
that ping-pongs per literal returns exactly the same rows.

The transport here is a loopback to a real ``InterpreterBackend``, so what these
tests exercise is the whole wire round trip and not a stub agreeing with itself.
"""

from __future__ import annotations

import json

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import (
    EdgeFact,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.records import records_to_json, refs_from_json
from finecode_knowledge.query.remote import RemoteBackend, RemoteQueryError
from finecode_knowledge.query.serialize import query_from_json, result_to_json
from finecode_knowledge.query.snapshot import registry_from_json, registry_to_json

_CATALOG = "libcat.catalog_scan"
_SHELVES = "libcat.shelf_survey"


def _prov(provider: str, line: int = 1) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=RunStamp(id="r", observed_at="t"),
        location=SourceLoc(project="ana", file="catalog.toml", line=line),
    )


@pytest.fixture
def store() -> FactStore:
    fact_store = FactStore(LIBCAT_SCHEMA)
    ana, bo = Author.ref(handle="ana"), Author.ref(handle="bo")
    a_book, b_book = Book.ref(isbn="a-1"), Book.ref(isbn="b-1")
    fact_store.ingest(
        _CATALOG,
        [
            FieldFact(entity=a_book, field="isbn", value="a-1", prov=_prov(_CATALOG)),
            FieldFact(
                entity=b_book, field="isbn", value="b-1", prov=_prov(_CATALOG, 2)
            ),
            EdgeFact(kind="wrote", src=ana, dst=a_book, prov=_prov(_CATALOG, 7)),
            EdgeFact(kind="wrote", src=bo, dst=b_book, prov=_prov(_CATALOG, 8)),
        ],
    )
    fact_store.ingest(
        _SHELVES,
        [
            EdgeFact(
                kind="shelved_on",
                src=a_book,
                dst=Shelf.ref(code="s-A"),
                prov=_prov(_SHELVES),
            )
        ],
    )
    return fact_store


class _Loopback:
    """The owner's side, reached through JSON exactly as it would be over a socket.

    It rebuilds the query against a registry rebuilt *from a snapshot*, so nothing
    here can accidentally share a live schema object with the caller -- which is
    the situation the WM is actually in.
    """

    def __init__(self, store: FactStore) -> None:
        self._store = store
        self._schema = registry_from_json(registry_to_json(LIBCAT_SCHEMA))
        self.received: list[dict] = []
        self.footprints: list[int] = []

    async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict:
        self.received.append(
            json.loads(json.dumps({"query": query, "mode": mode, "limit": limit}))
        )
        backend = InterpreterBackend(self._store, schema=self._schema)
        result = await backend.run(
            query_from_json(query, self._schema), mode=q.Mode(mode), limit=limit
        )
        self.footprints.append(len(backend.last_footprint))
        return json.loads(json.dumps(result_to_json(result)))

    async def fetch_records(self, refs: list[dict]) -> dict:
        self.received.append(json.loads(json.dumps({"refs": refs})))
        backend = InterpreterBackend(self._store, schema=self._schema)
        found = await backend.records(refs_from_json(refs))
        self.footprints.append(len(backend.last_footprint))
        return json.loads(json.dumps(records_to_json(found)))


# ---- substitutability ---------------------------------------------------


async def test_a_query_gets_the_same_rows_through_either_backend(
    store: FactStore,
) -> None:
    """The equivalence the whole split rests on. If shipping the query changed the
    answer, "who executes" would be a semantic decision rather than a placement one."""
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    built = q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )

    local = await built.all(InterpreterBackend(store))
    shipped = await built.all(RemoteBackend(_Loopback(store)))

    assert shipped.rows == local.rows
    assert shipped.freshness == local.freshness


async def test_a_rule_runs_unchanged_against_the_remote_backend(
    store: FactStore,
) -> None:
    """ADR-0016 D1/D2 made every terminal ``async`` for exactly this. The rule body
    does not know which backend it has, and there is no second spelling for it to
    pick."""

    @q.derived
    def _on_a_shelf(book: q.Var[Book], isbn: q.Var[str]) -> q.Body:
        """The existential, factored out -- negation is a single literal (FR3)."""
        return q.all_(Rel.shelved_on(book, q.var(Shelf)), Book.key(book, isbn=isbn))

    LIBCAT_SCHEMA.register_predicate(_on_a_shelf)

    @q.rule(id="libcat.unshelved_book")
    def unshelved(
        subject: q.Var[Author], missing: q.Var[str], asserted_at: q.Prov
    ) -> q.Body:
        """author {subject} wrote a book that is not on any shelf"""
        book = q.var(Book)
        return q.all_(
            Rel.wrote(subject, book, at=asserted_at),
            Book.key(book, isbn=missing),
            q.not_(_on_a_shelf(book, missing)),
        )

    local = await unshelved.violations(InterpreterBackend(store))
    shipped = await unshelved.violations(RemoteBackend(_Loopback(store)))

    assert [(v.subject, v.missing) for v in shipped.rows] == [("bo", "b-1")]
    assert shipped.rows == local.rows


async def test_a_bound_provenance_survives_the_crossing(store: FactStore) -> None:
    """A violation's location is most of its value, so a rule run through the owner
    must not produce visibly worse diagnostics than one run in process."""
    author, shelf, at = q.var(Author), q.var(Book), q.Prov()

    result = await (
        q.query(at)
        .where(Rel.wrote(author, shelf, at=at))
        .all(RemoteBackend(_Loopback(store)))
    )

    locations = sorted(row[0].location.line for row in result.rows)
    assert locations == [7, 8]
    assert all(isinstance(row[0], Provenance) for row in result.rows)


# ---- granularity: criterion 7 ------------------------------------------


async def test_one_execution_sends_exactly_one_message(store: FactStore) -> None:
    """§4.10's "not chatty" property. The unit of access is a whole query, so a rule
    joining four relations still costs one crossing -- a backend that ping-ponged per
    literal would return identical rows and be invisible without this."""
    author, book, shelf = q.var(Author), q.var(Book), q.var(Shelf)
    transport = _Loopback(store)
    backend = RemoteBackend(transport)

    await (
        q.query(author)
        .where(
            Rel.wrote(author, book),
            BookFields.isbn(book, q.var(str)),
            Rel.shelved_on(book, shelf),
        )
        .all(backend)
    )

    assert backend.messages_sent == 1
    assert len(transport.received) == 1


async def test_message_count_is_independent_of_result_size(store: FactStore) -> None:
    """Criterion 8 restated across the boundary: what must never happen is a message
    per traversal."""
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    built = q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )
    backend = RemoteBackend(_Loopback(store))

    two_rows = await built.all(backend)
    for extra in range(20):
        ref = Book.ref(isbn=f"i{extra}")
        store.ingest(
            _CATALOG,
            [
                FieldFact(
                    entity=ref, field="isbn", value=f"i{extra}", prov=_prov(_CATALOG)
                ),
                EdgeFact(
                    kind="wrote",
                    src=Author.ref(handle=f"a{extra}"),
                    dst=ref,
                    prov=_prov(_CATALOG),
                ),
            ],
            unit_id=f"u{extra}",
        )
    many_rows = await built.all(backend)

    assert (len(two_rows.rows), len(many_rows.rows)) == (2, 22)
    assert backend.messages_sent == 2, "one per execution, not one per row"


async def test_the_mode_and_limit_cross_because_the_caller_cannot_apply_them(
    store: FactStore,
) -> None:
    """``exists()`` asks for one row. Applying the limit on this side would mean
    receiving every row first, which is the transfer the whole design avoids."""
    author, book = q.var(Author), q.var(Book)
    transport = _Loopback(store)

    result = await (
        q.query(author)
        .where(Rel.wrote(author, book))
        .exists(RemoteBackend(transport), mode=q.Mode.CACHED)
    )

    assert result.value is True
    (sent,) = transport.received
    assert (sent["mode"], sent["limit"]) == ("cached", 1)


# ---- the footprint stays with the reader --------------------------------


async def test_the_footprint_stays_on_the_side_that_did_the_reading(
    store: FactStore,
) -> None:
    """R7/R21 stop being a convention here: the caller cannot record a footprint even
    if it wanted to, because it never touched a fact. ADR-0013 D5's collector is the
    executing side's, and nothing in the result carries one."""
    author, book = q.var(Author), q.var(Book)
    transport = _Loopback(store)
    backend = RemoteBackend(transport)

    await q.query(author).where(Rel.wrote(author, book)).all(backend)

    assert transport.footprints == [1], "the owner recorded the slot it scanned"
    assert not hasattr(backend, "last_footprint")


# ---- failure is not an empty result -------------------------------------


async def test_a_transport_failure_is_not_a_rule_that_passed(store: FactStore) -> None:
    """The failure direction that matters: a swallowed error and a query that matched
    nothing are the same value, and only one of them means there is no violation."""

    class _Broken:
        async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict:
            raise ConnectionError("socket closed")

    author, book = q.var(Author), q.var(Book)

    with pytest.raises(RemoteQueryError, match="did not answer"):
        await (
            q.query(author).where(Rel.wrote(author, book)).all(RemoteBackend(_Broken()))
        )


async def test_a_result_without_a_verdict_is_refused(store: FactStore) -> None:
    """R16: rows *and* a verdict, always. A response carrying only rows would give the
    caller a bare value where staleness was possible."""

    class _RowsOnly:
        async def run_query(self, query: dict, *, mode: str, limit: int | None) -> dict:
            return {"rows": []}

    author, book = q.var(Author), q.var(Book)

    with pytest.raises(RemoteQueryError, match="freshness verdict"):
        await (
            q.query(author)
            .where(Rel.wrote(author, book))
            .all(RemoteBackend(_RowsOnly()))
        )


# ---- the record read across the boundary (Phase 1b, R21) ---------------


async def test_records_come_back_identical_through_either_backend(
    store: FactStore,
) -> None:
    """Substitutability for the read that is *not* a query. A projection cannot
    tell which side answered, which is what lets ``which_handlers`` run both in an
    ER against the WM and standalone against a local file (criterion 10)."""
    refs = [Author.ref(handle="ana"), Book.ref(isbn="a-1")]

    local = await InterpreterBackend(store, schema=LIBCAT_SCHEMA).records(refs)
    shipped = await RemoteBackend(_Loopback(store)).records(refs)

    assert shipped == local


async def test_many_refs_cost_one_message(store: FactStore) -> None:
    """The batching is the whole reason the method takes a sequence. Per-ref, a
    projection over forty handlers would pay forty round trips -- criterion 8's
    "independent of result size", lost at the last step."""
    transport = _Loopback(store)
    backend = RemoteBackend(transport)

    await backend.records(
        [Author.ref(handle="ana"), Author.ref(handle="bo"), Book.ref(isbn="a-1")]
    )

    assert backend.messages_sent == 1
    assert len(transport.received) == 1


async def test_asking_for_no_records_sends_no_message(store: FactStore) -> None:
    """Asking for nothing is not a question, and a round trip that can only answer
    ``[]`` is one the caller pays for."""
    transport = _Loopback(store)
    backend = RemoteBackend(transport)

    assert await backend.records([]) == ()
    assert backend.messages_sent == 0
    assert transport.received == []


async def test_the_record_footprint_stays_on_the_side_that_did_the_reading(
    store: FactStore,
) -> None:
    """R7/R21 for this read, and it holds structurally: the asking side never
    touches a fact, so it has nothing to record. Before Phase 1b it held the
    ``FactSource`` and recorded nothing while reading everything."""
    transport = _Loopback(store)
    backend = RemoteBackend(transport)

    await backend.records([Author.ref(handle="ana"), Book.ref(isbn="a-1")])

    assert transport.footprints == [2]
    assert not hasattr(backend, "last_footprint")


async def test_a_short_reply_is_refused_rather_than_misaligned(
    store: FactStore,
) -> None:
    """The reply is positional, so a length mismatch would silently attribute one
    entity's fields to another -- a wrong answer that looks like a right one."""

    class _Truncating(_Loopback):
        async def fetch_records(self, refs: list[dict]) -> dict:
            full = await super().fetch_records(refs)
            return {"v": full["v"], "records": full["records"][:-1]}

    backend = RemoteBackend(_Truncating(store))

    with pytest.raises(RemoteQueryError, match="positional"):
        await backend.records([Author.ref(handle="ana"), Book.ref(isbn="a-1")])


async def test_a_transport_failure_on_a_record_read_is_not_an_empty_entity(
    store: FactStore,
) -> None:
    """Same argument as for a query: an entity nobody asserted anything about and
    a read that failed in transit are the same empty record if the failure is
    swallowed, and only one of them is an answer."""

    class _Broken(_Loopback):
        async def fetch_records(self, refs: list[dict]) -> dict:
            raise ConnectionError("the socket went away")

    with pytest.raises(RemoteQueryError, match="did not answer"):
        await RemoteBackend(_Broken(store)).records([Author.ref(handle="ana")])
