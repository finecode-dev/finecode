import pytest

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef, EntityType
from finecode_knowledge.model.errors import (
    BandViolationError,
    SchemaError,
    SuppliesViolationError,
)
from finecode_knowledge.model.fact_source import Revision
from finecode_knowledge.model.facts import EdgeFact, FieldFact, Provenance, RunStamp
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.relationship import Relationship
from finecode_knowledge.model.store import FactStore


class Widget(EntityType):
    NAME = "Widget"
    KEY = [Field("id", entity="Widget")]
    CORE = [
        Field("id", entity="Widget"),
        Field("name", entity="Widget"),
        Field("file_loc", entity="Widget"),
    ]


NAME_FIELD: Field[Widget, str] = Field("name", entity="Widget")
FILE_LOC_FIELD: Field[Widget, str] = Field("file_loc", entity="Widget")

SERVES: Relationship[Widget, Widget] = Relationship(
    "serves", "Widget", "Widget", band=Band.DECLARED, lower=0, upper=1
)


class WidgetProviderA(EntityProvider):
    ID = "provider_a"
    SUPPLIES_FIELDS = [NAME_FIELD, FILE_LOC_FIELD]
    SUPPLIES_EDGES = [SERVES]


class WidgetProviderB(EntityProvider):
    ID = "provider_b"
    SUPPLIES_FIELDS = [NAME_FIELD]
    SUPPLIES_EDGES = []


def _run() -> RunStamp:
    return RunStamp(id="r-1", observed_at="2026-07-13T10:00:00Z")


def _prov(provider: str, band: Band = Band.DECLARED) -> Provenance:
    return Provenance(band=band, provider=provider, run=_run(), location=None)


def _build_schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_field(NAME_FIELD, package="tests")
    schema.register_field(FILE_LOC_FIELD, package="tests")
    schema.register_relationship(SERVES, package="tests")
    schema.register_provider(WidgetProviderA)
    schema.register_provider(WidgetProviderB)
    return schema


def test_ingest_unsupplied_field_raises_supplies_violation_error() -> None:
    """A provider cannot silently write into a field it never declared it supplies -- SUPPLIES-bounding keeps provenance honest (C4)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    fact = FieldFact(
        entity=ref, field="file_loc", value="a.py:1", prov=_prov("tests.provider_b")
    )

    with pytest.raises(SuppliesViolationError):
        store.ingest("tests.provider_b", [fact])


def test_ingest_unsupplied_edge_kind_raises_supplies_violation_error() -> None:
    """A provider cannot emit an edge kind absent from its SUPPLIES_EDGES -- the same C4 discipline applies to edges as to fields."""
    schema = _build_schema()
    store = FactStore(schema)
    edge = EdgeFact(
        kind="serves",
        src=Widget.ref(id="w1"),
        dst=Widget.ref(id="w2"),
        prov=_prov("tests.provider_b"),
    )

    with pytest.raises(SuppliesViolationError):
        store.ingest("tests.provider_b", [edge])


def test_ingest_unregistered_provider_raises_schema_error() -> None:
    """Ingesting under a provider id the schema has never seen must fail -- an unregistered producer cannot silently land facts in the store."""
    schema = _build_schema()
    store = FactStore(schema)
    fact = FieldFact(
        entity=Widget.ref(id="w1"),
        field="name",
        value="X",
        prov=_prov("tests.ghost_provider"),
    )

    with pytest.raises(SchemaError):
        store.ingest("tests.ghost_provider", [fact])


def test_ingest_derived_band_field_fact_raises_band_violation_error() -> None:
    """A fact claiming DERIVED band can never be stored -- derived relations are rules evaluated at query time, never persisted facts (C5)."""
    schema = _build_schema()
    store = FactStore(schema)
    fact = FieldFact(
        entity=Widget.ref(id="w1"),
        field="name",
        value="X",
        prov=_prov("tests.provider_a", Band.DERIVED),
    )

    with pytest.raises(BandViolationError):
        store.ingest("tests.provider_a", [fact])


def test_ingest_derived_band_edge_fact_raises_band_violation_error() -> None:
    """The DERIVED-band rejection applies to edges as much as fields -- no stored specialized_by-style edge can sneak in through the front door (C5)."""
    schema = _build_schema()
    store = FactStore(schema)
    edge = EdgeFact(
        kind="serves",
        src=Widget.ref(id="w1"),
        dst=Widget.ref(id="w2"),
        prov=_prov("tests.provider_a", Band.DERIVED),
    )

    with pytest.raises(BandViolationError):
        store.ingest("tests.provider_a", [edge])


def test_ingest_unknown_entity_type_raises_schema_error() -> None:
    """A fact whose entity names a type the schema never registered is malformed -- addressing must go through a known type (C2)."""
    schema = _build_schema()
    store = FactStore(schema)
    bad_ref = EntityRef(type="tests.Ghost", key=("x",))
    fact = FieldFact(
        entity=bad_ref, field="name", value="X", prov=_prov("tests.provider_a")
    )

    with pytest.raises(SchemaError):
        store.ingest("tests.provider_a", [fact])


def test_ingest_wrong_key_arity_raises_schema_error() -> None:
    """A key tuple whose arity does not match the type's declared KEY length is not a valid address, so ingest must reject it (C2)."""
    schema = _build_schema()
    store = FactStore(schema)
    bad_ref = EntityRef(type=Widget.qualified_name(), key=("w1", "extra"))
    fact = FieldFact(
        entity=bad_ref, field="name", value="X", prov=_prov("tests.provider_a")
    )

    with pytest.raises(SchemaError):
        store.ingest("tests.provider_a", [fact])


def test_ingest_same_provider_twice_replaces_bucket_not_duplicates() -> None:
    """Re-ingesting the identical emissions from one provider must not accumulate duplicate facts -- extract_knowledge can be re-run safely (C7)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    fact = FieldFact(
        entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
    )

    store.ingest("tests.provider_a", [fact])
    store.ingest("tests.provider_a", [fact])

    assert store.record(ref).fields["tests.name"].value == "X"


def test_ingest_removes_facts_dropped_from_second_emission() -> None:
    """A field present in a provider's first ingest but absent from its second must disappear from the record -- ingest replaces the bucket, it does not accumulate it (C7)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    prov = _prov("tests.provider_a")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(entity=ref, field="name", value="X", prov=prov),
            FieldFact(entity=ref, field="file_loc", value="a.py:1", prov=prov),
        ],
    )

    store.ingest(
        "tests.provider_a", [FieldFact(entity=ref, field="name", value="X", prov=prov)]
    )

    record = store.record(ref).fields
    assert "tests.file_loc" not in record
    assert record["tests.name"].value == "X"


def test_ingest_leaves_other_providers_facts_untouched() -> None:
    """Replacing one provider's bucket must not disturb facts owned by any other provider -- bucket ownership is per-provider (C7)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref,
                field="file_loc",
                value="a.py:1",
                prov=_prov("tests.provider_a"),
            )
        ],
    )
    store.ingest(
        "tests.provider_b",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_b")
            )
        ],
    )

    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref,
                field="file_loc",
                value="a.py:2",
                prov=_prov("tests.provider_a"),
            )
        ],
    )

    record = store.record(ref).fields
    assert record["tests.name"].value == "X"
    assert record["tests.file_loc"].value == "a.py:2"


def test_record_merges_fields_across_providers_with_distinct_provenance() -> None:
    """record() merges disjoint fields supplied by different providers into one entity, each cell carrying its own provider's provenance -- the visible C8 sparse merge."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref,
                field="file_loc",
                value="a.py:1",
                prov=_prov("tests.provider_a"),
            )
        ],
    )
    store.ingest(
        "tests.provider_b",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_b")
            )
        ],
    )

    record = store.record(ref).fields

    assert record["tests.name"].prov.provider == "tests.provider_b"
    assert record["tests.file_loc"].prov.provider == "tests.provider_a"


def test_record_omits_unsupplied_fields() -> None:
    """A field no provider supplied is absent from the record dict, never present with a null placeholder (absent != null, C8)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref,
                field="file_loc",
                value="a.py:1",
                prov=_prov("tests.provider_a"),
            )
        ],
    )

    record = store.record(ref).fields

    assert "name" not in record
    assert set(record.keys()) == {"tests.file_loc"}


def test_record_of_unknown_ref_returns_empty_dict() -> None:
    """Asking for the record of an entity the store has never ingested returns an empty dict, not an error or a stub record."""
    schema = _build_schema()
    store = FactStore(schema)

    assert store.record(Widget.ref(id="does-not-exist")).fields == {}


def test_record_idempotent_equal_values_from_two_providers_yields_one_entry() -> None:
    """Two providers independently reporting the identical value for the same field is not a conflict -- it is the expected shape of the ~30-duplicate-row registry case (C9)."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )
    store.ingest(
        "tests.provider_b",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_b")
            )
        ],
    )

    assert store.record(ref).fields["tests.name"].value == "X"


def test_record_carries_conflicting_values_from_two_providers_instead_of_raising() -> (
    None
):
    """Two providers disagreeing on a field surface as a carried `Conflict`, never as a silently
    picked winner and no longer as an exception (C9, ADR-0014 D5).

    Raising destroyed a whole audit's output over one contested field somewhere in the
    touched set -- trading a complete answer for no answer. Both values are kept, each with
    its own provenance, so the disagreement names which providers disagree and where each
    spoke."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )
    store.ingest(
        "tests.provider_b",
        [
            FieldFact(
                entity=ref, field="name", value="Y", prov=_prov("tests.provider_b")
            )
        ],
    )

    record = store.record(ref)

    (conflict,) = record.conflicts
    assert conflict.entity == ref
    assert conflict.field == "tests.name"
    assert {v.value for v in conflict.values} == {"X", "Y"}
    assert {v.prov.provider for v in conflict.values} == {
        "tests.provider_a",
        "tests.provider_b",
    }


def test_conflicts_are_detected_at_ingest_not_from_the_scan_the_interpreter_walks() -> (
    None
):
    """A *value-bound* consultation still sees the conflict (ADR-0014 D4).

    This is why detection lives at ingest. Detecting from the scan the interpreter
    already walks is cheaper and was the first design, but a value-bound literal
    scans only facts whose value matches -- so a provider asserting a *different*
    value for the same slot is not in that scan and its conflict is invisible,
    yielding a result that claims `verified` over a contested input."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )
    store.ingest(
        "tests.provider_b",
        [
            FieldFact(
                entity=ref, field="name", value="Y", prov=_prov("tests.provider_b")
            )
        ],
    )

    # The scan a literal `NAME_FIELD(w, "X")` would walk contains only the "X" fact...
    assert [
        f.value
        for f in store.field_facts(Widget.qualified_name(), "tests.name", value="X")
    ] == ["X"]
    # ...yet the slot is still reported contested.
    (conflict,) = store.conflicts(Widget.qualified_name(), "tests.name", entity=ref)
    assert {v.value for v in conflict.values} == {"X", "Y"}


def test_conflicts_is_empty_for_an_uncontested_slot() -> None:
    """The common case costs nothing and reports nothing."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )

    assert list(store.conflicts(Widget.qualified_name(), "tests.name")) == []
    assert store.record(ref).conflicts == ()


def test_a_derived_traversal_reads_serves_edges_without_storing_specialized_by() -> (
    None
):
    """The inverse of a stored edge is computed at call time -- no `specialized_by` edge
    is ever persisted, which keeps derived relations rules rather than duplicated facts
    (P8). This is the traversal `predicates.specialized_by` now expresses as a query."""
    schema = _build_schema()
    store = FactStore(schema)
    parent = Widget.ref(id="parent")
    child = Widget.ref(id="child")
    store.ingest(
        "tests.provider_a",
        [
            EdgeFact(
                kind="serves", src=child, dst=parent, prov=_prov("tests.provider_a")
            )
        ],
    )

    sources = [e.src for e in store.edge_facts("tests.serves", dst=parent)]

    assert sources == [child]
    assert list(store.edge_facts("tests.specialized_by")) == []


def test_entities_of_type_returns_all_ingested_refs() -> None:
    """entities_of_type lists every distinct ref that has received at least one fact of that type, the enumeration surface the projection layer walks."""
    schema = _build_schema()
    store = FactStore(schema)
    ref1 = Widget.ref(id="w1")
    ref2 = Widget.ref(id="w2")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref1, field="name", value="X", prov=_prov("tests.provider_a")
            ),
            FieldFact(
                entity=ref2, field="name", value="Y", prov=_prov("tests.provider_a")
            ),
        ],
    )

    assert set(store.entities_of_type(Widget.qualified_name())) == {ref1, ref2}


def test_field_facts_value_bound_returns_matching_facts() -> None:
    """A value-bound field scan resolves an entity by any supplied field value, not only its key -- this is how which_handlers resolves an action's alias, and it returns whole facts so the provenance FR5 binds is not thrown away."""
    schema = _build_schema()
    store = FactStore(schema)
    ref1 = Widget.ref(id="w1")
    ref2 = Widget.ref(id="w2")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref1, field="name", value="X", prov=_prov("tests.provider_a")
            ),
            FieldFact(
                entity=ref2, field="name", value="Y", prov=_prov("tests.provider_a")
            ),
        ],
    )

    assert [
        f.entity
        for f in store.field_facts(Widget.qualified_name(), "tests.name", value="X")
    ] == [ref1]


def test_edges_filters_by_src_and_dst() -> None:
    """edges() with a dst filter returns only edges pointing at that destination, and with a src filter only edges leaving that source -- the two filters are independent."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b, c = Widget.ref(id="a"), Widget.ref(id="b"), Widget.ref(id="c")
    store.ingest(
        "tests.provider_a",
        [
            EdgeFact(kind="serves", src=a, dst=b, prov=_prov("tests.provider_a")),
            EdgeFact(kind="serves", src=c, dst=b, prov=_prov("tests.provider_a")),
        ],
    )

    by_dst = list(store.edge_facts("tests.serves", dst=b))
    by_src = list(store.edge_facts("tests.serves", src=a))

    assert {e.src for e in by_dst} == {a, c}
    assert {e.dst for e in by_src} == {b}


def test_edge_facts_src_bound_returns_destinations() -> None:
    """The forward traversal used to resolve a handler's runs_in environment from an edge, not from a handler field."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b = Widget.ref(id="a"), Widget.ref(id="b")
    store.ingest(
        "tests.provider_a",
        [EdgeFact(kind="serves", src=a, dst=b, prov=_prov("tests.provider_a"))],
    )

    assert [e.dst for e in store.edge_facts("tests.serves", src=a)] == [b]


def test_edge_facts_dst_bound_returns_sources() -> None:
    """The reverse traversal SpecializedBy relies on to recover subactions from stored serves edges."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b = Widget.ref(id="a"), Widget.ref(id="b")
    store.ingest(
        "tests.provider_a",
        [EdgeFact(kind="serves", src=a, dst=b, prov=_prov("tests.provider_a"))],
    )

    assert [e.src for e in store.edge_facts("tests.serves", dst=b)] == [a]


def test_wire_round_trip_reproduces_record_and_edges_including_buckets() -> None:
    """FactStore.from_json(schema, store.to_json()) reproduces identical query results and provider bucket ownership, so a re-ingest after loading from disk still respects C7."""
    schema = _build_schema()
    store = FactStore(schema)
    ref, other = Widget.ref(id="w1"), Widget.ref(id="w2")
    prov = _prov("tests.provider_a")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(entity=ref, field="name", value="X", prov=prov),
            EdgeFact(kind="serves", src=ref, dst=other, prov=prov),
        ],
    )

    loaded = FactStore.from_json(schema, store.to_json())

    assert loaded.record(ref).fields == store.record(ref).fields
    assert set(loaded.edge_facts("tests.serves")) == set(
        store.edge_facts("tests.serves")
    )

    loaded.ingest(
        "tests.provider_a", [FieldFact(entity=ref, field="name", value="Z", prov=prov)]
    )
    assert loaded.record(ref).fields["tests.name"].value == "Z"


class Gadget(EntityType):
    NAME = "Gadget"
    KEY = [Field("id", entity="Gadget")]
    CORE = [Field("id", entity="Gadget"), Field("label", entity="Gadget")]


GADGET_LABEL_FIELD: Field[Gadget, str] = Field("label", entity="Gadget")


class GadgetProvider(EntityProvider):
    ID = "gadget_provider"
    SUPPLIES_FIELDS = [GADGET_LABEL_FIELD]
    SUPPLIES_EDGES = []


def _dedup_schema() -> SchemaRegistry:
    schema = SchemaRegistry()
    schema.register_entity_type(Gadget)
    schema.register_field(GADGET_LABEL_FIELD, package="tests")
    schema.register_provider(GadgetProvider)
    return schema


def _gadget_prov() -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=GadgetProvider.qualified_id(),
        run=RunStamp(id="r-dedup", observed_at="2026-07-14T10:00:00Z"),
    )


def _gadget_label_fact(gadget_id: str, label: str) -> FieldFact:
    return FieldFact(
        entity=Gadget.ref(id=gadget_id),
        field="label",
        value=label,
        prov=_gadget_prov(),
    )


def test_ingest_collapses_exact_duplicate_emissions_keeping_first_occurrence_order() -> (
    None
):
    """A provider that emits the same fact many times leaves the store holding one copy per distinct fact, in emission order -- this is what keeps the fact file small enough that a query does not pay a multi-second load (D1)."""
    schema = _dedup_schema()
    alpha = _gadget_label_fact("g1", "alpha")
    beta = _gadget_label_fact("g2", "beta")

    duplicated_store = FactStore(schema)
    stored_count = duplicated_store.ingest(
        GadgetProvider.qualified_id(), [alpha, beta, alpha, alpha, beta]
    )

    unique_store = FactStore(schema)
    unique_store.ingest(GadgetProvider.qualified_id(), [alpha, beta])

    assert stored_count == 2
    assert duplicated_store.to_json()["facts"] == unique_store.to_json()["facts"]
    assert [
        (b["provider"], b["unit"], b["count"])
        for b in duplicated_store.to_json()["buckets"]
    ] == [(GadgetProvider.qualified_id(), "__all__", 2)]


def test_record_is_identical_whether_or_not_duplicates_were_ingested() -> None:
    """Collapsing duplicate emissions changes nothing an operator can observe through a query -- this equivalence is what licenses compaction at ingest at all."""
    schema = _dedup_schema()
    alpha = _gadget_label_fact("g1", "alpha")

    duplicated_store = FactStore(schema)
    duplicated_store.ingest(GadgetProvider.qualified_id(), [alpha, alpha, alpha, alpha])

    unique_store = FactStore(schema)
    unique_store.ingest(GadgetProvider.qualified_id(), [alpha])

    ref = Gadget.ref(id="g1")
    assert duplicated_store.record(ref).fields == unique_store.record(ref).fields
    assert duplicated_store.record(ref).fields["tests.label"].value == "alpha"


def test_re_ingesting_a_provider_replaces_its_bucket_instead_of_accumulating() -> None:
    """Re-running a provider yields the same store as running it once on a fresh store, so repeated extraction never grows the fact file with stale entities (C7)."""
    schema = _dedup_schema()
    store = FactStore(schema)

    first_count = store.ingest(
        GadgetProvider.qualified_id(), [_gadget_label_fact("g1", "alpha")] * 3
    )
    second_count = store.ingest(
        GadgetProvider.qualified_id(), [_gadget_label_fact("g2", "beta")] * 5
    )

    assert first_count == 1
    assert second_count == 1
    assert [
        (b["provider"], b["unit"], b["count"]) for b in store.to_json()["buckets"]
    ] == [(GadgetProvider.qualified_id(), "__all__", 1)]
    assert list(store.entities_of_type(Gadget.qualified_name())) == [
        Gadget.ref(id="g2")
    ]


def test_ingest_replaces_only_the_named_unit_leaving_other_units_of_same_provider_intact() -> (
    None
):
    """Re-ingesting one unit of a provider must not wipe another unit's facts (T3/F1) -- the direct fix for "one changed file invalidates everything the provider ever emitted." Without a unit dimension, `ingest` replaced the whole provider bucket on every call."""
    schema = _dedup_schema()
    store = FactStore(schema)
    store.ingest(
        GadgetProvider.qualified_id(),
        [_gadget_label_fact("g1", "alpha")],
        unit_id="file_a.py",
    )
    store.ingest(
        GadgetProvider.qualified_id(),
        [_gadget_label_fact("g2", "beta")],
        unit_id="file_b.py",
    )

    store.ingest(
        GadgetProvider.qualified_id(),
        [_gadget_label_fact("g1", "alpha-renamed")],
        unit_id="file_a.py",
    )

    assert (
        store.record(Gadget.ref(id="g1")).fields["tests.label"].value == "alpha-renamed"
    )
    assert store.record(Gadget.ref(id="g2")).fields["tests.label"].value == "beta"


def test_ingest_defaults_to_whole_provider_unit_when_unit_id_omitted() -> None:
    """A caller that never passes `unit_id` keeps the pre-T3 whole-provider-bucket behavior (the DEFAULT_UNIT_ID shim) -- ingesting again without a unit_id still replaces everything the provider previously reported, not just adds to it."""
    schema = _dedup_schema()
    store = FactStore(schema)
    store.ingest(GadgetProvider.qualified_id(), [_gadget_label_fact("g1", "alpha")])

    store.ingest(GadgetProvider.qualified_id(), [_gadget_label_fact("g2", "beta")])

    assert list(store.entities_of_type(Gadget.qualified_name())) == [
        Gadget.ref(id="g2")
    ]


def test_conflicting_values_for_one_field_survive_duplicate_collapse() -> None:
    """Two different values for the same entity field stay contested even when duplicates
    surround them -- compaction must never quietly pick a winner for genuinely disagreeing
    sources (C9)."""
    schema = _dedup_schema()
    store = FactStore(schema)
    agreed = _gadget_label_fact("g1", "alpha")
    conflicting = _gadget_label_fact("g1", "renamed")

    store.ingest(GadgetProvider.qualified_id(), [agreed, agreed, conflicting, agreed])

    (conflict,) = store.record(Gadget.ref(id="g1")).conflicts
    assert {v.value for v in conflict.values} == {"alpha", "renamed"}


# ---- ADR-0013 D3/D6: revision, contains, and the laziness contract ----


def test_revision_is_stable_across_reads_and_identifies_the_content_served() -> None:
    """A revision names exactly the facts served (ADR-0013 D6.2). Reading does not
    change it; ingesting different content does."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )

    first = store.revision
    assert store.revision == first  # reading is not a mutation

    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="Y", prov=_prov("tests.provider_a")
            )
        ],
    )
    assert store.revision != first


def test_revision_is_insensitive_to_ingest_order() -> None:
    """Two stores holding the same facts serve the same content, so they must report the
    same revision -- otherwise a digest comparison would report drift that does not exist."""
    fact_a = FieldFact(
        entity=Widget.ref(id="w1"),
        field="name",
        value="X",
        prov=_prov("tests.provider_a"),
    )
    fact_b = FieldFact(
        entity=Widget.ref(id="w2"),
        field="name",
        value="Y",
        prov=_prov("tests.provider_a"),
    )

    one = FactStore(_build_schema())
    one.ingest("tests.provider_a", [fact_a], unit_id="u1")
    one.ingest("tests.provider_a", [fact_b], unit_id="u2")

    other = FactStore(_build_schema())
    other.ingest("tests.provider_a", [fact_b], unit_id="u2")
    other.ingest("tests.provider_a", [fact_a], unit_id="u1")

    assert one.revision == other.revision


def test_from_json_pins_the_supplied_revision() -> None:
    """The file-loaded path's revision is the fact file's own content digest (D6.2)."""
    schema = _build_schema()
    store = FactStore(schema)
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=Widget.ref(id="w1"),
                field="name",
                value="X",
                prov=_prov("tests.provider_a"),
            )
        ],
    )

    loaded = FactStore.from_json(
        schema, store.to_json(), revision=Revision("digest-from-file")
    )

    assert loaded.revision == "digest-from-file"


def test_contains_answers_existence_without_materializing_the_population() -> None:
    """The one existence question the scans cannot answer -- "does this entity have any
    facts at all" (ADR-0013 D3). `projection.py` used to materialize every Action to ask it."""
    schema = _build_schema()
    store = FactStore(schema)
    ref = Widget.ref(id="w1")
    store.ingest(
        "tests.provider_a",
        [
            FieldFact(
                entity=ref, field="name", value="X", prov=_prov("tests.provider_a")
            )
        ],
    )

    assert store.contains(Widget.qualified_name(), ref)
    assert not store.contains(Widget.qualified_name(), Widget.ref(id="ghost"))


def test_an_abandoned_scan_and_a_drained_scan_consult_the_same_slot() -> None:
    """Early termination must be safe (ADR-0013 D4.3): `any(...)` and `list(...)` reach the
    same index slot, so they record the same footprint key. If a partial scan could record
    less, a rule that stopped after one row would under-record its dependencies."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b, c = Widget.ref(id="a"), Widget.ref(id="b"), Widget.ref(id="c")
    store.ingest(
        "tests.provider_a",
        [
            EdgeFact(kind="serves", src=a, dst=c, prov=_prov("tests.provider_a")),
            EdgeFact(kind="serves", src=b, dst=c, prov=_prov("tests.provider_a")),
        ],
    )

    abandoned = store.edge_facts("tests.serves", dst=c)
    assert next(abandoned) is not None
    assert len(list(store.edge_facts("tests.serves", dst=c))) == 2


def test_scans_are_iterators_so_a_second_pass_is_empty() -> None:
    """The rename from `edges` to `edge_facts` is deliberate cover for this: no existing
    call site silently gets a one-shot iterator where it expected a list."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b = Widget.ref(id="a"), Widget.ref(id="b")
    store.ingest(
        "tests.provider_a",
        [EdgeFact(kind="serves", src=a, dst=b, prov=_prov("tests.provider_a"))],
    )

    scan = store.edge_facts("tests.serves")
    assert len(list(scan)) == 1
    assert list(scan) == []


def test_indexes_are_rebuilt_after_a_bucket_is_re_ingested() -> None:
    """The invalidation event is a `(provider, unit)` bucket ingest (ADR-0013 D4). A read
    taken before the re-ingest must not leave a stale index behind it."""
    schema = _build_schema()
    store = FactStore(schema)
    a, b = Widget.ref(id="a"), Widget.ref(id="b")
    store.ingest(
        "tests.provider_a",
        [EdgeFact(kind="serves", src=a, dst=b, prov=_prov("tests.provider_a"))],
    )
    assert len(list(store.edge_facts("tests.serves"))) == 1

    store.ingest("tests.provider_a", [])

    assert list(store.edge_facts("tests.serves")) == []
