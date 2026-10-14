"""Ownership units at the store's own level (R6, R11, ADR-0023, ADR-0026).

``goals.md`` §4.6 defines the unit as the provider's captured footprint, and
``FactStore.ingest`` takes one. What lives here is the half that is the
*engine's*: per-unit bucket replacement, the provider/unit agreement check, the
path frame, and ``source_inputs``' per-member module resolution.

The other half -- which files a *particular* provider scans, and how a handler
splits a workspace into units -- is a property of that provider, and its tests
stay with the package that declares it.
"""

from __future__ import annotations

import pytest
from libcat import LIBCAT_SCHEMA, Book, BookFields

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit, relative_to_workspace

PROVIDER = "libcat.catalog_scan"
OTHER = "libcat.author_index"


def _fact(isbn: str) -> FieldFact:
    return FieldFact(
        entity=Book.ref(isbn=isbn),
        field=BookFields.isbn.id,
        value=isbn,
        prov=Provenance(
            band=Band.DECLARED, provider=PROVIDER, run=RunStamp(id="r", observed_at="t")
        ),
    )


def test_ingesting_one_unit_leaves_the_provider_s_other_units_intact():
    """The guard R6 claims: bucket replacement is per unit, not per provider."""
    store = FactStore(LIBCAT_SCHEMA)

    store.ingest(PROVIDER, [_fact("a-1")], unit=Unit(PROVIDER, "shelfA/catalog.toml"))
    store.ingest(PROVIDER, [_fact("b-1")], unit=Unit(PROVIDER, "shelfB/catalog.toml"))
    store.ingest(PROVIDER, [_fact("a-2")], unit=Unit(PROVIDER, "shelfA/catalog.toml"))

    isbns = {
        f.value
        for f in store.field_facts(
            Book.qualified_name(), BookFields.isbn.qualified_name
        )
    }
    assert isbns == {"a-2", "b-1"}


def test_a_unit_ingested_under_the_wrong_provider_is_rejected():
    """The bucket key would name one provider and its declared inputs another."""
    store = FactStore(LIBCAT_SCHEMA)
    with pytest.raises(Exception, match="belongs to"):
        store.ingest(PROVIDER, [], unit=Unit(OTHER, "x.toml"))


def test_a_path_outside_the_workspace_keeps_its_absolute_frame(tmp_path):
    """ADR-0023 D3: a provider installed as a wheel has its code outside the root,
    and stays on the same code path rather than branching on install mode."""
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "provider.py"
    outside.parent.mkdir()
    outside.touch()

    assert relative_to_workspace(root / "a" / "b.py", root) == "a/b.py"
    assert relative_to_workspace(outside, root) == outside.resolve().as_posix()


# ---- ADR-0026 D2: the module is resolved per supplied member ------------


def test_a_providers_source_inputs_are_its_own_module_plus_each_supplied_members():
    """ADR-0023 D1 and ADR-0026 D1 together: a provider's code and the module
    declaring each field it emits are both inputs, because either can change what was
    extracted while every scanned file stays byte-identical."""
    paths = LIBCAT_SCHEMA.provider(PROVIDER).source_inputs(LIBCAT_SCHEMA)

    names = {p.name for p in paths}
    assert names == {"schema.py"}, (
        "libcat declares its providers in the same module as its fields, so both "
        "resolve to one file -- de-duplicated in first-seen order"
    )


def test_a_third_party_provider_gets_its_own_schema_module_not_the_declaring_ones():
    """D2's whole reason for existing.

    ``annex_ext`` declares ``Author.lending_ban`` in its own package and supplies
    only that. Fingerprinting a well-known ``libcat/schema.py`` would be correct for
    first-party providers and **wrong here** -- it would track a file this provider
    does not read and miss the one that decides what it emits.
    """
    from annex_ext.rules import AnnexAuditProvider

    paths = AnnexAuditProvider.source_inputs(LIBCAT_SCHEMA)

    assert [p.name for p in paths] == ["rules.py"]
    assert paths[0].parent.name == "annex_ext"
    assert not any(p.name == "schema.py" for p in paths)


def test_a_provider_supplying_from_two_vocabularies_tracks_both_modules():
    """The mixed case: a package that emits another's fields *and* its own. Resolving
    per member is what gets this right; a single well-known path cannot."""
    from annex_ext.rules import AnnexAuthorFields

    class MixedProvider(EntityProvider):
        ID = "mixed"
        SUPPLIES_FIELDS = [BookFields.isbn, AnnexAuthorFields.lending_ban]
        SUPPLIES_EDGES = []

    names = {p.name for p in MixedProvider.source_inputs(LIBCAT_SCHEMA)}

    assert "schema.py" in names, "the libcat module declaring Book.isbn"
    assert "rules.py" in names, "annex_ext's module declaring Author.lending_ban"
    assert "test_units.py" in names, "and the provider's own module"
