"""The engine holds no privileged path (R18, R19) -- and acceptance criterion 11.

``fine_knowledge``'s own suite has a fixture that looks like this test and is not:
``sec_ext`` registers *into* ``FINECODE_SCHEMA`` and extends core's ``Project``,
which proves a third party can extend the **built-in** schema. It does not prove
a schema can exist with *no* built-in schema present -- and that is now a
reachable state, because the WM's environment holds the engine and no schema at
all (memo-dag-plan Phase 0).

So the premise is asserted first and explicitly: **nothing named
``fine_knowledge`` is importable in this process**. Everything below then runs
against a schema declared entirely outside the engine, plus a second package
extending *that* one, on the terms R19 guarantees and no others.
"""

from __future__ import annotations

import importlib.util

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.errors import SchemaError, SuppliesViolationError
from finecode_knowledge.model.facts import (
    EdgeFact,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)
from finecode_knowledge.model.registry import SchemaRegistry, default_registry
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.query.interpret import InterpreterBackend

_CATALOG = "libcat.catalog_scan"
_SHELVES = "libcat.shelf_survey"
_ANNEX = "annex_ext.annex_audit"


def _prov(provider: str, *, line: int = 1) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=provider,
        run=RunStamp(id="r", observed_at="t"),
        location=SourceLoc(project="annex", file="catalog.toml", line=line),
    )


# ---- the premise -------------------------------------------------------


def test_no_finecode_schema_package_is_installed() -> None:
    """Criterion 11's premise, asserted rather than assumed.

    Every test in this suite is only evidence for R18/R19 while this holds: an
    engine bug that reaches for core's vocabulary is invisible in a process where
    core's vocabulary happens to be importable.
    """
    assert importlib.util.find_spec("fine_knowledge") is None, (
        "the engine's suite must run with no schema package installed; with one "
        "present a leaked assumption about its names would pass by coincidence"
    )


def test_the_process_default_registry_is_the_one_that_declared_itself() -> None:
    """The engine has no schema of its own to fall back to (R20), so the default is
    whatever nominated itself -- here ``libcat``, and nothing else could have."""
    assert default_registry() is LIBCAT_SCHEMA


def test_a_registry_nobody_nominated_has_no_default_to_guess() -> None:
    """A rule declared without ``schema=`` against a registry that never nominated
    itself has nothing to validate against, and says so rather than guessing."""
    fresh = SchemaRegistry()

    with pytest.raises(SchemaError, match="Unregistered entity type"):
        fresh.entity_type("libcat.Author")


# ---- R18: registration from outside, with no core edit -----------------


def test_a_second_package_registers_into_a_schema_it_did_not_declare() -> None:
    """R18. ``annex_ext`` adds a field, a provider, a predicate and a rule; not one of
    them required an edit to ``libcat`` or to the engine."""
    from annex_ext.rules import AnnexAuditProvider, AnnexAuthorFields

    described = LIBCAT_SCHEMA.describe()

    assert AnnexAuthorFields.lending_ban.qualified_name == "annex_ext.lending_ban"
    assert LIBCAT_SCHEMA.provider(_ANNEX) is AnnexAuditProvider
    assert "annex_ext.banned_author_on_shelf" in described["derived_predicates"]
    assert {"libcat", "annex_ext"} <= set(described["packages"])


def test_a_third_party_field_is_qualified_by_its_own_package_not_the_entitys() -> None:
    """ADR-0017 D3/D4: the qualifier is *derived*, so ``annex_ext`` cannot claim
    ``libcat.`` for its own field -- it never types the prefix."""
    from annex_ext.rules import AnnexAuthorFields

    field = AnnexAuthorFields.lending_ban

    assert field.package == "annex_ext"
    assert field.entity == Author.qualified_name() == "libcat.Author"
    assert field.qualified_name.startswith("annex_ext.")


def test_the_declaring_package_cannot_write_the_extending_packages_field() -> None:
    """The write-path half of the same ownership boundary: SUPPLIES-bounding (C4)
    knows nothing about who owns the *entity*, so ``libcat``'s providers cannot
    supply ``annex_ext``'s field on its behalf."""
    from annex_ext.rules import AnnexAuthorFields

    store = FactStore(LIBCAT_SCHEMA)

    with pytest.raises(SuppliesViolationError, match="does not supply"):
        store.ingest(
            _CATALOG,
            [
                FieldFact(
                    entity=Author.ref(handle="ana"),
                    field=AnnexAuthorFields.lending_ban.id,
                    value="yes",
                    prov=_prov(_CATALOG),
                )
            ],
        )


# ---- R19: no privileged path, end to end -------------------------------


@pytest.fixture
def store() -> FactStore:
    """``ana`` is banned and still has a shelved book; ``bo`` is not banned."""
    from annex_ext.rules import AnnexAuthorFields

    fact_store = FactStore(LIBCAT_SCHEMA)
    ana, bo = Author.ref(handle="ana"), Author.ref(handle="bo")
    a_book, b_book = Book.ref(isbn="a-1"), Book.ref(isbn="b-1")
    shelf = Shelf.ref(code="s-A")

    fact_store.ingest(
        _CATALOG,
        [
            FieldFact(entity=a_book, field="isbn", value="a-1", prov=_prov(_CATALOG)),
            FieldFact(entity=b_book, field="isbn", value="b-1", prov=_prov(_CATALOG)),
            EdgeFact(kind="wrote", src=ana, dst=a_book, prov=_prov(_CATALOG, line=7)),
            EdgeFact(kind="wrote", src=bo, dst=b_book, prov=_prov(_CATALOG, line=8)),
        ],
    )
    fact_store.ingest(
        _SHELVES,
        [
            FieldFact(entity=shelf, field="code", value="s-A", prov=_prov(_SHELVES)),
            EdgeFact(kind="shelved_on", src=a_book, dst=shelf, prov=_prov(_SHELVES)),
            EdgeFact(kind="shelved_on", src=b_book, dst=shelf, prov=_prov(_SHELVES)),
        ],
    )
    fact_store.ingest(
        _ANNEX,
        [
            FieldFact(
                entity=ana,
                field=AnnexAuthorFields.lending_ban.id,
                value="yes",
                prov=_prov(_ANNEX),
            )
        ],
    )
    return fact_store


async def test_a_rule_declared_outside_the_schema_package_runs_end_to_end(
    store: FactStore,
) -> None:
    """R19's whole claim, executing: a rule whose body mixes another package's derived
    predicate with its own field literal produces violations through the ordinary
    path, with no branch anywhere that knows it is third-party."""
    from annex_ext.rules import banned_author_still_shelved

    result = await banned_author_still_shelved.violations(InterpreterBackend(store))

    assert [(v.subject, v.missing) for v in result.rows] == [("ana", "s-A")]
    (violation,) = result.rows
    assert violation.rule == "annex_ext.banned_author_still_shelved"
    assert violation.asserted_at == SourceLoc(
        project="annex", file="catalog.toml", line=7
    ), "the Prov head parameter carries the fact's own location (ADR-0018 D1)"


async def test_the_extending_packages_predicate_is_callable_from_an_ordinary_query(
    store: FactStore,
) -> None:
    """Called by qualified name off the registry, exactly as a first-party predicate
    is -- the lookup is the only access R19 guarantees, and it is enough."""
    predicate = LIBCAT_SCHEMA.predicate("annex_ext.banned_author_on_shelf")
    author, shelf = q.var(Author), q.var(Shelf)

    result = (
        await q.query(author, shelf)
        .where(predicate(author, shelf))
        .all(InterpreterBackend(store))
    )

    assert result.rows == [(Author.ref(handle="ana"), Shelf.ref(code="s-A"))]


async def test_first_and_third_party_literals_are_indistinguishable_in_one_body(
    store: FactStore,
) -> None:
    """FR2 across a package boundary: the body cannot tell which package declared
    which literal, and neither can the walk."""
    from annex_ext.rules import AnnexAuthorFields

    author, book = q.var(Author), q.var(Book)

    result = await (
        q.query(author)
        .where(
            Rel.wrote(author, book),
            AnnexAuthorFields.lending_ban(author, "yes"),
        )
        .all(InterpreterBackend(store))
    )

    assert result.rows == [(Author.ref(handle="ana"),)]
