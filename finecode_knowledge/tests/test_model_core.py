import pytest

from finecode_knowledge.model.bands import MANY, Band
from finecode_knowledge.model.entity_type import EntityRef, EntityType
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import Provenance, RunStamp
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.relationship import AnyRelationship, Relationship


class Widget(EntityType):
    NAME = "Widget"
    KEY = [Field("id", entity="Widget")]
    CORE = [Field("id", entity="Widget")]


class Gizmo(EntityType):
    NAME = "Gizmo"
    KEY = [Field("id", entity="Gizmo")]
    CORE = [Field("id", entity="Gizmo")]


class Handler2Key(EntityType):
    NAME = "Handler2Key"
    KEY = [Field("source", entity="Handler2Key"), Field("env", entity="Handler2Key")]
    CORE = []


CONTAINS: Relationship[Widget, Gizmo] = Relationship(
    "contains", "Widget", "Gizmo", band=Band.DECLARED, lower=0, upper=MANY
)


class WidgetProvider(EntityProvider):
    ID = "widget_provider"
    SUPPLIES_FIELDS = [Field("id", entity="Widget")]
    SUPPLIES_EDGES = [CONTAINS]


# Refs are qualified from construction while a freshly-built Relationship carries
# local endpoints, so `CONTAINS.edge(...)` can only match once something has bound
# it to a declaring package. Registration is that step (ADR-0017 D3) -- the cost
# the ADR records as "ad-hoc construction gains a required `package=`".
_BOUND = SchemaRegistry()
for _t in (Widget, Gizmo, Handler2Key):
    _BOUND.register_entity_type(_t)
_BOUND.register_relationship(CONTAINS, package="tests")


def _prov() -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider="widget_provider",
        run=RunStamp(id="r-1", observed_at="2026-07-13T10:00:00Z"),
        location=None,
    )


def _flatten_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            found |= _flatten_strings(key)
            found |= _flatten_strings(item)
        return found
    if isinstance(value, (list, tuple, set)):
        found = set()
        for item in value:
            found |= _flatten_strings(item)
        return found
    return set()


def test_field_equality_by_id_and_entity() -> None:
    """Two Field instances naming the same (id, entity) pair are interchangeable, so registries and dicts can dedupe by value instead of identity."""
    a = Field("name", entity="Action")
    b = Field("name", entity="Action")

    assert a == b
    assert hash(a) == hash(b)


def test_field_str_format() -> None:
    """A Field renders as '<Entity>.<id>' so log lines and error messages name the exact column without ad-hoc formatting at each call site."""
    field = Field("name", entity="Action")

    assert str(field) == "Action.name"


def test_entity_type_ref_returns_entityref_with_single_key() -> None:
    """EntityType.ref() addresses a single-key entity by wrapping the sole key value in a one-tuple, giving typed addressing over the raw dict projection (C12)."""
    ref = Widget.ref(id="w1")

    assert ref == EntityRef(type=Widget.qualified_name(), key=("w1",))


def test_entity_type_ref_builds_composite_key_in_declared_order() -> None:
    """A composite-key type's ref() always orders the tuple by KEY declaration order, independent of kwarg call order, so refs stay comparable across call sites."""
    ref_a = Handler2Key.ref(env="dev", source="pkg.Handler")
    ref_b = Handler2Key.ref(source="pkg.Handler", env="dev")

    assert (
        ref_a
        == ref_b
        == EntityRef(type=Handler2Key.qualified_name(), key=("pkg.Handler", "dev"))
    )


def test_entity_type_ref_missing_key_raises_schema_error() -> None:
    """Omitting a required key component must fail loudly rather than silently produce a partial, unaddressable ref."""
    with pytest.raises(SchemaError):
        Handler2Key.ref(source="pkg.Handler")


def test_entity_type_ref_unknown_key_name_raises_schema_error() -> None:
    """Passing a key name the type never declared must fail, catching a caller typo instead of silently building a wrong ref."""
    with pytest.raises(SchemaError):
        Widget.ref(id="w1", bogus="x")


def test_entity_type_ref_coerces_key_values_to_str() -> None:
    """Key values are coerced to str, so a caller passing an int id still produces a ref comparable to one built from a string id."""
    ref = Widget.ref(id=42)

    assert ref == EntityRef(type=Widget.qualified_name(), key=("42",))


def test_relationship_edge_returns_edgefact_for_matching_endpoints() -> None:
    """A relationship whose src/dst types match the given refs produces the edge fact -- the normal, unremarkable path."""
    src = Widget.ref(id="w1")
    dst = Gizmo.ref(id="g1")
    prov = _prov()

    edge = CONTAINS.edge(src, dst, prov)

    assert edge.kind == "contains"
    assert edge.src == src
    assert edge.dst == dst
    assert edge.prov == prov


def test_relationship_edge_wrong_src_type_raises_schema_error() -> None:
    """A wrong-endpoint edge is rejected at construction time, not silently accepted and only noticed downstream (C6)."""
    src = Gizmo.ref(id="g1")
    dst = Gizmo.ref(id="g2")

    with pytest.raises(SchemaError):
        CONTAINS.edge(src, dst, _prov())


def test_relationship_edge_wrong_dst_type_raises_schema_error() -> None:
    """A wrong-endpoint edge is rejected at construction time regardless of which side is wrong (C6)."""
    src = Widget.ref(id="w1")
    dst = Widget.ref(id="w2")

    with pytest.raises(SchemaError):
        CONTAINS.edge(src, dst, _prov())


def test_schema_registry_register_field_on_unregistered_type_raises() -> None:
    """A field cannot be attached to a type the registry has never seen -- this catches a typo'd entity name at registration time, not at query time."""
    schema = SchemaRegistry()

    with pytest.raises(SchemaError):
        schema.register_field(Field("owner", entity="Ghost"), package="tests")


def test_schema_registry_register_field_twice_is_idempotent() -> None:
    """Registering the identical field twice must not create a duplicate entry -- callers can register defensively without checking first."""
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)

    schema.register_field(Field("label", entity="Widget"), package="tests")
    schema.register_field(Field("label", entity="Widget"), package="tests")

    assert [f.id for f in schema.fields_of(Widget.qualified_name())].count("label") == 1


def test_schema_registry_third_party_field_registration_extends_fields_of() -> None:
    """A field registered by code that never touches the type's own source still shows up in fields_of() -- the vocabulary is open by construction (P1)."""
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)

    schema.register_field(Field("owner", entity="Widget"), package="tests")

    field_ids = {f.id for f in schema.fields_of(Widget.qualified_name())}
    assert "owner" in field_ids


def test_schema_registry_register_relationship_unregistered_src_raises() -> None:
    """A relationship naming an src type the registry has not seen must fail at registration, before any fact can reference it."""
    schema = SchemaRegistry()
    schema.register_entity_type(Gizmo)
    bad_rel: AnyRelationship = Relationship(
        "contains", "Widget", "Gizmo", band=Band.DECLARED
    )

    with pytest.raises(SchemaError):
        schema.register_relationship(bad_rel, package="tests")


def test_schema_registry_register_relationship_unregistered_dst_raises() -> None:
    """A relationship naming a dst type the registry has not seen must fail at registration, before any fact can reference it."""
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    bad_rel: AnyRelationship = Relationship(
        "contains", "Widget", "Gizmo", band=Band.DECLARED
    )

    with pytest.raises(SchemaError):
        schema.register_relationship(bad_rel, package="tests")


def test_describe_reports_every_registered_type_field_edge_and_provider() -> None:
    """describe() is the one call an operator uses to answer "what does this schema know about" -- every registration must be visible there, in one call (P11)."""
    schema = SchemaRegistry()
    schema.register_entity_type(Widget)
    schema.register_field(Field("id", entity="Widget"), package="tests")
    schema.register_field(Field("label", entity="Widget"), package="tests")
    schema.register_entity_type(Gizmo)
    schema.register_relationship(CONTAINS, package="tests")
    schema.register_provider(WidgetProvider)

    described = schema.describe()
    strings = _flatten_strings(described)

    assert "tests.Widget" in strings
    assert "tests.Gizmo" in strings
    assert "tests.label" in strings
    assert "tests.contains" in strings
    assert "tests.widget_provider" in strings


def test_schema_registry_instances_are_independent() -> None:
    """SchemaRegistry holds no global state -- two registries built in the same process must not see each other's registrations, which is what keeps the model unit-testable without a shared singleton."""
    schema_one = SchemaRegistry()
    schema_two = SchemaRegistry()
    schema_one.register_entity_type(Widget)

    with pytest.raises(SchemaError):
        schema_two.register_field(Field("id", entity="Widget"), package="tests")
