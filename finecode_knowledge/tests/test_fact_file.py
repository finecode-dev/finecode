from pathlib import Path

import pytest

from finecode_knowledge.fact_file import read_facts, write_facts
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityType
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp, SourceLoc
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.store import FactStore


class Widget(EntityType):
    NAME = "Widget"
    KEY = [Field("id", entity="Widget")]
    CORE = [Field("id", entity="Widget"), Field("label", entity="Widget")]


LABEL_FIELD = Field("label", entity="Widget")


class WidgetProvider(EntityProvider):
    ID = "widget_provider"
    SUPPLIES_FIELDS = [LABEL_FIELD]
    SUPPLIES_EDGES = []


def _build_schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_field(LABEL_FIELD, package="tests")
    schema.register_provider(WidgetProvider)
    return schema


def test_write_facts_then_read_facts_reproduces_record(tmp_path: Path) -> None:
    """A fact file written by write_facts and reloaded by read_facts answers record() identically, so extract_knowledge and which_handlers can run as separate processes without losing provenance."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    fact = FieldFact(
        entity=ref,
        field="label",
        value="Widget One",
        prov=Provenance(
            band=Band.DECLARED,
            provider="tests.widget_provider",
            run=RunStamp(id="r-1", observed_at="2026-07-13T10:00:00Z"),
            location=None,
        ),
    )
    store.ingest("tests.widget_provider", [fact])
    target_path = tmp_path / "nested" / "facts.json"

    count = write_facts(store, target_path)

    assert target_path.exists()
    assert count == 1

    loaded_store = read_facts(schema, target_path)

    assert loaded_store.record(ref) == store.record(ref)


def test_write_facts_creates_missing_parent_directories(tmp_path: Path) -> None:
    """write_facts must create the parent directories of the target path, so callers do not need a pre-existing .finecode/knowledge/ directory."""
    schema = _build_schema()
    store = FactStore(schema)
    target_path = tmp_path / "does" / "not" / "exist" / "facts.json"

    write_facts(store, target_path)

    assert target_path.parent.is_dir()


def test_read_facts_on_missing_path_raises_file_not_found_error(tmp_path: Path) -> None:
    """which_handlers must fail loudly when extract_knowledge has never run, not silently fabricate an empty knowledge store (decision A)."""
    schema = _build_schema()
    missing_path = tmp_path / "does-not-exist" / "facts.json"

    with pytest.raises(FileNotFoundError):
        read_facts(schema, missing_path)


class Sprocket(EntityType):
    NAME = "Sprocket"
    KEY = [Field("id", entity="Sprocket")]
    CORE = [Field("id", entity="Sprocket"), Field("size", entity="Sprocket")]


SIZE_FIELD = Field("size", entity="Sprocket")


class SprocketProvider(EntityProvider):
    ID = "sprocket_provider"
    SUPPLIES_FIELDS = [SIZE_FIELD]
    SUPPLIES_EDGES = []


def _two_provider_schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_entity_type(Sprocket)
    schema.register_field(LABEL_FIELD, package="tests")
    schema.register_field(SIZE_FIELD, package="tests")
    schema.register_provider(WidgetProvider)
    schema.register_provider(SprocketProvider)
    return schema


def _two_provider_store_with_duplicates(schema: SchemaRegistry) -> FactStore:
    run = RunStamp(id="r-two", observed_at="2026-07-14T10:00:00Z")
    widget_prov = Provenance(
        band=Band.DECLARED,
        provider=WidgetProvider.qualified_id(),
        run=run,
        location=SourceLoc(project="widgets", file="registry.py", line=7),
    )
    sprocket_prov = Provenance(
        band=Band.DECLARED,
        provider=SprocketProvider.qualified_id(),
        run=run,
        location=SourceLoc(project="sprockets", file="registry.py", line=11),
    )
    widget_one = FieldFact(
        entity=Widget.ref(id="w1"), field="label", value="alpha", prov=widget_prov
    )
    widget_two = FieldFact(
        entity=Widget.ref(id="w2"), field="label", value="beta", prov=widget_prov
    )
    sprocket_one = FieldFact(
        entity=Sprocket.ref(id="s1"), field="size", value="m4", prov=sprocket_prov
    )
    sprocket_two = FieldFact(
        entity=Sprocket.ref(id="s2"), field="size", value="m6", prov=sprocket_prov
    )

    store = FactStore(schema)
    store.ingest(
        WidgetProvider.qualified_id(),
        [widget_one, widget_two, widget_one, widget_one, widget_two],
    )
    store.ingest(
        SprocketProvider.qualified_id(),
        [sprocket_one, sprocket_one, sprocket_two, sprocket_one],
    )
    return store


def test_two_provider_fact_file_round_trip_keeps_each_provider_bucket_intact(
    tmp_path: Path,
) -> None:
    """A reloaded fact file attributes every fact back to the provider that emitted it, even when both providers emitted duplicates -- if bucket boundaries slip, facts get silently credited to the wrong provider and provenance becomes a lie."""
    schema = _two_provider_schema()
    store = _two_provider_store_with_duplicates(schema)
    path = tmp_path / "facts.json"

    written = write_facts(store, path)
    reloaded = read_facts(schema, path)

    assert written == 4
    assert {(b["provider"], b["count"]) for b in reloaded.to_json()["buckets"]} == {
        (WidgetProvider.qualified_id(), 2),
        (SprocketProvider.qualified_id(), 2),
    }
    assert reloaded.to_json()["facts"] == store.to_json()["facts"]


def test_two_provider_fact_file_round_trip_reproduces_record_for_both_entity_types(
    tmp_path: Path,
) -> None:
    """Every entity answers a query after a write/read cycle exactly as it did in memory, so a query process reading a compacted fact file returns the same answers as the process that produced it."""
    schema = _two_provider_schema()
    store = _two_provider_store_with_duplicates(schema)
    path = tmp_path / "facts.json"

    write_facts(store, path)
    reloaded = read_facts(schema, path)

    assert reloaded.record(Widget.ref(id="w1")) == store.record(Widget.ref(id="w1"))
    assert reloaded.record(Widget.ref(id="w2")) == store.record(Widget.ref(id="w2"))
    assert reloaded.record(Sprocket.ref(id="s1")) == store.record(Sprocket.ref(id="s1"))
    assert reloaded.record(Sprocket.ref(id="s2")) == store.record(Sprocket.ref(id="s2"))
    assert reloaded.record(Sprocket.ref(id="s1")).fields["tests.size"].value == "m4"
