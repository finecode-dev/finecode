"""The registry as data, so the executing side can hold one without importing one (D-7).

The claim these tests have to establish is stronger than "the fields survive": a
registry rebuilt from a snapshot must be able to **execute a query** -- resolve
key orders, attribute footprint keys to providers, and expand derived predicate
bodies -- in a process where the package that declared the schema was never
imported. The last test does exactly that, in a subprocess, and it is the one
that would catch a snapshot that quietly depended on the original objects still
being alive.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Copy, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import EdgeFact, FieldFact, Provenance, RunStamp
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.query import footprint as fp
from finecode_knowledge.query.attribution import providers_for_key
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.serialize import query_to_json
from finecode_knowledge.query.snapshot import (
    SnapshotError,
    registry_from_json,
    registry_to_json,
)

_CATALOG = "libcat.catalog_scan"


@pytest.fixture
def snapshot() -> dict:
    # `annex_ext` is imported for its side effect: it registers a field, a
    # provider and a predicate into LIBCAT_SCHEMA from outside it, and the point
    # of a snapshot built from the *registry* is that it cannot miss them.
    import annex_ext.rules  # noqa: F401

    return registry_to_json(LIBCAT_SCHEMA)


@pytest.fixture
def rebuilt(snapshot: dict):
    return registry_from_json(snapshot)


def _prov() -> Provenance:
    return Provenance(
        band=Band.DECLARED, provider=_CATALOG, run=RunStamp(id="r", observed_at="t")
    )


# ---- faithfulness -------------------------------------------------------


def test_a_rebuilt_registry_describes_itself_identically(rebuilt) -> None:
    """``describe()`` is the registry's own account of what it holds, so equality here
    covers entity types, fields, relationships, derived predicates, providers and the
    package grouping in one assertion."""
    assert rebuilt.describe() == LIBCAT_SCHEMA.describe()


def test_the_snapshot_is_json(snapshot: dict) -> None:
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_qualified_names_survive_without_the_declaring_module(rebuilt) -> None:
    """Every qualified name is derived from the declaring class's ``__module__``
    (ADR-0017 D3). The rebuilt classes carry the declaring *package* as their module,
    so the qualifier is reproduced rather than re-derived from whatever imported it."""
    author = rebuilt.entity_type("libcat.Author")

    assert author.qualified_name() == "libcat.Author"
    assert author.ref(handle="ana") == Author.ref(handle="ana")


def test_a_composite_key_keeps_its_order(rebuilt) -> None:
    """``KEY`` order is positional: ``ref()`` builds the key tuple from it, so a
    reordered snapshot would build references that silently name other entities."""
    copy = rebuilt.entity_type("libcat.Copy")

    assert [f.id for f in copy.KEY] == [f.id for f in Copy.KEY] == ["isbn", "branch"]
    assert copy.ref(isbn="a-1", branch="north") == Copy.ref(isbn="a-1", branch="north")


def test_a_third_partys_declarations_are_in_the_snapshot_like_everyone_elses(
    rebuilt,
) -> None:
    """R18/R19 across the boundary. The snapshot is built from the registry, not from a
    list of schema modules, so a member registered from outside cannot be omitted."""
    from annex_ext.rules import AnnexAuthorFields

    field = next(
        f for f in rebuilt.fields() if f.qualified_name == "annex_ext.lending_ban"
    )

    assert field.entity == "libcat.Author"
    assert field.package == "annex_ext"
    assert AnnexAuthorFields.lending_ban.qualified_name == field.qualified_name
    assert [field] == rebuilt.provider("annex_ext.annex_audit").SUPPLIES_FIELDS


def test_relationship_cardinality_and_band_survive(rebuilt) -> None:
    wrote = rebuilt.relationship("libcat.wrote")
    cites = rebuilt.relationship("libcat.cites")

    assert (wrote.src, wrote.dst, wrote.band, wrote.upper) == (
        Rel.wrote.src,
        Rel.wrote.dst,
        Band.DECLARED,
        1,
    )
    assert cites.band is Band.SEMI_DECLARED


def test_a_derived_predicates_body_crosses_because_the_executing_side_expands_it(
    rebuilt,
) -> None:
    """The query carries a predicate by *name* (§5.9); the bodies cross once, here.
    Without them the receiving side could resolve the name and then have nothing to
    expand."""
    original = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book").predicate
    restored = rebuilt.predicate("libcat.shelves_a_book").predicate

    assert restored.params == original.params
    assert len(restored.clauses) == len(original.clauses)
    assert [
        [literal.predicate for literal in clause.body] for clause in restored.clauses
    ] == [[literal.predicate for literal in clause.body] for clause in original.clauses]


def test_a_multi_clause_predicate_keeps_both_clauses(rebuilt) -> None:
    restored = rebuilt.predicate("libcat.cited_or_written").predicate

    assert len(restored.clauses) == 2
    assert {clause.body.literals[0].predicate for clause in restored.clauses} == {
        Rel.wrote.qualified_name,
        Rel.borrowed.qualified_name,
    }


# ---- what a rebuilt registry deliberately cannot do ---------------------


def test_a_rebuilt_predicate_is_not_callable(rebuilt) -> None:
    """A rule body that could call it would be a rule body running on the side that
    holds no rule code -- the one thing Q3b keeps out of this process."""
    predicate = rebuilt.predicate("libcat.shelves_a_book")

    assert not callable(predicate)


def test_a_rebuilt_provider_refuses_to_report_source_inputs(rebuilt) -> None:
    """``source_inputs`` reads modules on disk. On this side there are none, and
    returning an empty tuple would look like "this provider has no code inputs" --
    which is the untracked-but-not-declared state R9 forbids."""
    provider = rebuilt.provider(_CATALOG)

    with pytest.raises(SnapshotError, match="no code on this side"):
        provider.source_inputs(rebuilt)


def test_a_snapshot_from_a_future_version_is_refused_by_name(snapshot: dict) -> None:
    with pytest.raises(SnapshotError, match="version 99"):
        registry_from_json({**snapshot, "v": 99})


def test_a_snapshot_naming_a_field_it_does_not_carry_is_refused(snapshot: dict) -> None:
    """Loud rather than partial: a registry missing a field it was asked for would
    fail later, at a query, with nothing pointing back at the snapshot."""
    broken = json.loads(json.dumps(snapshot))
    broken["fields"] = [f for f in broken["fields"] if f["id"] != "handle"]

    with pytest.raises(SnapshotError, match="no such field"):
        registry_from_json(broken)


# ---- the point: it executes --------------------------------------------


async def test_a_query_executes_against_a_rebuilt_registry(rebuilt) -> None:
    """Every read the walk makes against the schema, exercised at once: the field
    literal's entity type, the edge kind, and the ``KEY`` literal's key order."""
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        _CATALOG,
        [
            FieldFact(
                entity=Book.ref(isbn="a-1"), field="isbn", value="a-1", prov=_prov()
            ),
            EdgeFact(
                kind="wrote",
                src=Author.ref(handle="ana"),
                dst=Book.ref(isbn="a-1"),
                prov=_prov(),
            ),
        ],
    )
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    wire = query_to_json(
        q.query(author, isbn).where(Rel.wrote(author, book), Book.key(book, isbn=isbn))
    )

    backend = InterpreterBackend(store, schema=rebuilt)
    result = await backend.run(q.query_from_json(wire, rebuilt), mode=q.Mode.VERIFIED)

    assert result.value == [(Author.ref(handle="ana"), "a-1")]


async def test_a_derived_predicate_expands_against_a_rebuilt_registry(rebuilt) -> None:
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        _CATALOG,
        [
            EdgeFact(
                kind="wrote",
                src=Author.ref(handle="ana"),
                dst=Book.ref(isbn="a-1"),
                prov=_prov(),
            )
        ],
    )
    store.ingest(
        "libcat.shelf_survey",
        [
            EdgeFact(
                kind="shelved_on",
                src=Book.ref(isbn="a-1"),
                dst=Shelf.ref(code="s-A"),
                prov=Provenance(
                    band=Band.DECLARED,
                    provider="libcat.shelf_survey",
                    run=RunStamp(id="r", observed_at="t"),
                ),
            )
        ],
    )
    predicate = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book")
    author, shelf = q.var(Author), q.var(Shelf)
    wire = query_to_json(q.query(author, shelf).where(predicate(author, shelf)))

    backend = InterpreterBackend(store, schema=rebuilt)
    result = await backend.run(q.query_from_json(wire, rebuilt), mode=q.Mode.VERIFIED)

    assert result.value == [(Author.ref(handle="ana"), Shelf.ref(code="s-A"))]


def test_attribution_runs_against_a_rebuilt_registry(rebuilt) -> None:
    """The half of the memo the snapshot exists for: without ``SUPPLIES`` on this side
    the executing process could run a query and not know which buckets its answer
    rested on."""
    key = fp.field_key(
        Book.qualified_name(), BookFields.isbn.qualified_name, None, None
    )

    assert providers_for_key(key, rebuilt) == providers_for_key(key, LIBCAT_SCHEMA)
    assert providers_for_key(key, rebuilt) == {_CATALOG}


def test_a_snapshot_rebuilds_in_a_process_that_never_imported_the_schema(
    snapshot: dict, tmp_path
) -> None:
    """**The test the fixtures cannot fake.** Everything above runs in a process where
    ``libcat`` is imported, so a snapshot that quietly leaned on the live objects would
    pass. Here the schema package is not even on ``sys.path``: only the engine and the
    JSON are.

    This is the WM's actual situation (memo-dag-plan Phase 0/1.2), and criterion 11's
    shape one layer up.
    """
    payload = tmp_path / "snapshot.json"
    payload.write_text(json.dumps(snapshot))
    script = tmp_path / "run.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, importlib.util, pathlib
            assert importlib.util.find_spec("libcat") is None, "the premise failed"

            from finecode_knowledge.query.snapshot import registry_from_json
            from finecode_knowledge.query import footprint as fp
            from finecode_knowledge.query.attribution import providers_for_key

            registry = registry_from_json(json.loads(pathlib.Path({str(payload)!r}).read_text()))
            author = registry.entity_type("libcat.Author")
            assert author.qualified_name() == "libcat.Author"
            assert author.ref(handle="ana").key == ("ana",)
            assert [f.id for f in registry.entity_type("libcat.Copy").KEY] == ["isbn", "branch"]
            assert registry.predicate("libcat.shelves_a_book").predicate.clauses
            key = fp.field_key("libcat.Book", "libcat.isbn", None, None)
            assert providers_for_key(key, registry) == {{"libcat.catalog_scan"}}
            print("ok")
            """
        )
    )

    completed = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
