"""A ``SchemaRegistry`` as data, so the WM can hold one without importing one.

``goals.md`` §4.10 puts the store and the query walk in the WM; ``Q3b`` keeps rule
*code* in the ER. That leaves the WM needing the schema and forbidden from
importing it -- attribution reads every provider's ``SUPPLIES`` lists
(``query/attribution.py``), the ``KEY`` literal needs each entity type's key
order (``interpret._match_key``), and expansion needs every derived predicate's
body (``interpret._match_derived``).

**None of that needs code.** What those three read is *data*: names, key orders,
supplies lists, and IR. So the ER sends a snapshot once, at registration, and the
WM rebuilds a registry from it. Arbitrary code stays out of the WM process, and a
third-party schema participates identically (R18/R19), because a snapshot does
not care who declared it.

## What is faithful, and what is not

Rebuilt: entity types (name, key order, core fields), fields, relationships
(endpoints, band, cardinality), providers (``SUPPLIES``, ``DETERMINISTIC``), and
derived predicates *with their bodies*.

Not rebuilt: **rules**, deliberately -- the ER runs those and sends the query they
compile to; **provider behaviour**, because a snapshot provider is a declaration,
not an extractor, and the WM never extracts; **``source_inputs``**, which reads
modules on disk and is an extraction-time concern.

A rebuilt provider therefore raises rather than pretending it can extract. The
one thing the WM must never be able to do by accident is run somebody's code, and
a class that *looks* runnable is how that happens.

## Why this lives in ``query/`` and not in ``model/``

A snapshot is mostly registry data, which would put it in ``model/`` -- but a
derived predicate's body is IR whose terms are ``Var``/``Prov``, and those live in
``query/terms.py``. Import direction is one-way, ``query/`` -> ``model/`` and
never the reverse (§5.10), so the module that needs both goes on the ``query/``
side. The alternative was a deferred import pointing the wrong way, which is
exactly the shape of the R20 back-edges this package was split to remove.

## Qualification survives because the module name is set, not because it is guessed

Every qualified name in the registry is derived from the declaring class's
``__module__`` (ADR-0017 D3). The rebuilt classes carry the declaring package as
their ``__module__``, so ``declaring_package`` returns exactly what it returned on
the sending side -- and ``fine_knowledge.Package`` stays ``fine_knowledge.Package``
in a process where nothing called ``fine_knowledge`` exists.
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityType
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.naming import declaring_package, split_qualified
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.relationship import Relationship
from finecode_knowledge.query.ir_wire import predicate_from_json, predicate_to_json
from finecode_knowledge.query.terms import Prov
from finecode_knowledge.query.version import clauses_version_hash

if typing.TYPE_CHECKING:
    import pathlib

    from finecode_knowledge.model.literal import Predicate

__all__ = ["SnapshotError", "registry_from_json", "registry_to_json"]

SNAPSHOT_VERSION = 1


class SnapshotError(SchemaError):
    """A snapshot that cannot be read, named precisely enough to act on."""


# ---- outbound ----------------------------------------------------------


def registry_to_json(registry: SchemaRegistry) -> dict:
    """*registry* as JSON. Sent once, at registration.

    Reads the registry's own namespaces rather than a hand-listed set of schema
    modules: a member is registered by existing (ADR-0017 D2), so a snapshot
    built from the registry cannot omit a field somebody added and forgot to
    list here.
    """
    entity_types = [
        {
            "package": declaring_package(entity_type),
            "name": entity_type.NAME,
            "key": [f.id for f in entity_type.KEY],
            "core": [f.id for f in entity_type.CORE],
        }
        for entity_type in registry.entity_types()
    ]

    fields = [
        {"package": field.package, "entity": field.entity, "id": field.id}
        for field in registry.fields()
    ]

    relationships = [
        {
            "package": relationship.package,
            "kind": relationship.kind,
            "src": relationship.src,
            "dst": relationship.dst,
            "band": relationship.band.value,
            "lower": relationship.lower,
            "upper": relationship.upper,
            "opposite": relationship.opposite,
            "containment": relationship.containment,
        }
        for relationship in registry.relationships()
    ]

    predicates = [
        {
            "shape": predicate.shape.value,
            "endpoints": list(predicate.endpoints),
            **predicate_to_json(predicate.predicate),
        }
        for predicate in registry.derived_predicates()
    ]

    providers = [
        {
            "package": split_qualified(provider.qualified_id())[0],
            "id": provider.ID,
            "supplies_fields": [
                {"entity": f.entity, "id": f.id, "package": f.package}
                for f in provider.SUPPLIES_FIELDS
            ],
            "supplies_edges": [r.qualified_name for r in provider.SUPPLIES_EDGES],
            "deterministic": provider.DETERMINISTIC,
        }
        for provider in registry.providers()
    ]

    return {
        "v": SNAPSHOT_VERSION,
        "entity_types": entity_types,
        "fields": fields,
        "relationships": relationships,
        "derived_predicates": predicates,
        "providers": providers,
    }


# ---- inbound -----------------------------------------------------------


class _SnapshotPredicate:
    """A derived predicate with a body and no callable behind it.

    ``DerivedPredicate`` infers its head from a Python ``def``; there is no
    ``def`` here and inventing one would mean executing code the snapshot
    carried. What the executing side actually reads is ``.predicate`` -- the IR
    it expands -- so that is what this provides, plus the two attributes
    ``SchemaRegistry.describe`` needs to render it.

    It is **not callable**: a rule body that could call it would be a rule body
    running in the WM, which is the one thing Q3b keeps out of this process.
    """

    __slots__ = ("_predicate", "endpoints", "id", "shape")

    def __init__(self, predicate: Predicate, shape: object, endpoints: tuple) -> None:
        self.id = predicate.id
        self._predicate = predicate
        self.shape = shape
        self.endpoints = endpoints

    @property
    def params(self) -> tuple[str, ...]:
        return self._predicate.params

    @property
    def predicate(self) -> Predicate:
        return self._predicate

    @property
    def version_hash(self) -> str:
        """R8's code-version row, recomputed from the rebuilt IR.

        Recomputed rather than shipped as a field: the hash is a function of the
        clause IR, and the IR crosses. Sending it as well would create a second
        source of truth that a version skew could silently split -- the receiver
        would trust a number that no longer described the bodies beside it.
        """
        return clauses_version_hash(self._predicate.clauses)

    @property
    def location_sensitive(self) -> bool:
        """ADR-0027 D3's classification, read off the rebuilt head.

        ``DerivedPredicate`` tests the head *annotations*; this tests the head
        *terms*, and the two agree by construction -- ``_fresh_term`` builds a
        ``Prov()`` exactly where the annotation was ``Prov``. Deriving it here
        rather than shipping a flag keeps the wire form from carrying a claim the
        IR beside it could contradict.
        """
        return any(
            isinstance(term, Prov)
            for clause in self._predicate.clauses
            for term in clause.head
        )

    def __repr__(self) -> str:
        return f"_SnapshotPredicate({self.id!r}, params={self.params})"


class _SnapshotShape:
    """``PredicateShape``'s wire value, kept as a value.

    Importing ``query.predicate.PredicateShape`` here would point ``model/`` at
    ``query/``, and that import direction is one-way by design (§5.10).
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"PredicateShape.{self.value.upper()}"


def _rebuild_entity_type(
    spec: dict, fields: dict[tuple[str, str], Field]
) -> type[EntityType]:
    package, name = spec["package"], spec["name"]

    def _pick(field_id: str) -> Field:
        found = fields.get((f"{package}.{name}", field_id))
        if found is None:
            raise SnapshotError(
                f"Entity type {package}.{name} names field {field_id!r} in KEY/CORE, "
                "but the snapshot carries no such field."
            )
        return found

    rebuilt = type(
        name,
        (EntityType,),
        {
            "NAME": name,
            "KEY": [_pick(f) for f in spec["key"]],
            "CORE": [_pick(f) for f in spec["core"]],
            "__module__": package,
            "__doc__": f"{package}.{name}, rebuilt from a schema snapshot.",
        },
    )
    return typing.cast("type[EntityType]", rebuilt)


def _unbound(spec: dict) -> Field:
    """A ``Field`` with its qualifier already applied.

    The registry binds a field's package at registration by deriving it from the
    declaring class -- there is no declaring class here, so the values are set
    directly and ``register_field(package=...)`` is handed the same package. The
    entity is already qualified in the snapshot, and ``qualify`` passes a
    qualified name through unchanged.
    """
    return Field(id=spec["id"], entity=spec["entity"], package=spec["package"])


def _rebuild_provider(
    spec: dict, fields: dict[tuple[str, str], Field], registry: SchemaRegistry
):
    package = spec["package"]
    supplies_fields = []
    for field_spec in spec["supplies_fields"]:
        found = fields.get((field_spec["entity"], field_spec["id"]))
        if found is None:
            raise SnapshotError(
                f"Provider {package}.{spec['id']} supplies "
                f"{field_spec['entity']}.{field_spec['id']}, which the snapshot does not declare."
            )
        supplies_fields.append(found)

    def _source_inputs(cls, schema: SchemaRegistry) -> tuple[pathlib.Path, ...]:
        raise SnapshotError(
            f"{package}.{spec['id']} was rebuilt from a schema snapshot and has no code "
            "on this side. Source inputs are an extraction-time concern, and extraction "
            "runs where the provider actually lives."
        )

    rebuilt = type(
        spec["id"],
        (EntityProvider,),
        {
            "ID": spec["id"],
            "SUPPLIES_FIELDS": supplies_fields,
            "SUPPLIES_EDGES": [
                registry.relationship(k) for k in spec["supplies_edges"]
            ],
            "DETERMINISTIC": spec["deterministic"],
            "source_inputs": classmethod(_source_inputs),
            "__module__": package,
            "__doc__": (
                f"{package}.{spec['id']}, rebuilt from a schema snapshot: a declaration "
                "of what it may supply, never an extractor."
            ),
        },
    )
    return rebuilt


def registry_from_json(data: dict) -> SchemaRegistry:
    """Rebuild a registry from *data*.

    The order is the order a schema module declares things in, and for the same
    reason: a field names its entity type, a relationship names both endpoints,
    and a provider names the fields and kinds it supplies -- so each must already
    be registered when the next one is read.

    Raises:
        SnapshotError: *data* is not a snapshot this version can read, or names a
            member it does not itself carry.
    """
    version = data.get("v")
    if version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"Unsupported schema-snapshot version {version!r}; this build reads "
            f"version {SNAPSHOT_VERSION}."
        )

    registry = SchemaRegistry()

    fields = {(spec["entity"], spec["id"]): _unbound(spec) for spec in data["fields"]}

    # Entity types first: a field cannot be registered against an entity type the
    # registry has not seen, and `register_entity_type` registers the type's own
    # KEY/CORE fields as it goes.
    for spec in data["entity_types"]:
        registry.register_entity_type(_rebuild_entity_type(spec, fields))
    for (entity, _field_id), field in fields.items():
        if field.package is None:
            raise SnapshotError(
                f"Field {entity}.{field.id} carries no declaring package."
            )
        registry.register_field(field, package=field.package)

    for spec in data["relationships"]:
        registry.register_relationship(
            Relationship(
                kind=spec["kind"],
                src=spec["src"],
                dst=spec["dst"],
                band=Band(spec["band"]),
                lower=spec["lower"],
                upper=spec["upper"],
                opposite=spec["opposite"],
                containment=spec["containment"],
                package=spec["package"],
            ),
            package=spec["package"],
        )

    for spec in data["derived_predicates"]:
        registry.register_predicate(
            typing.cast(
                typing.Any,
                _SnapshotPredicate(
                    predicate_from_json(spec, registry),
                    shape=_SnapshotShape(spec["shape"]),
                    endpoints=tuple(spec["endpoints"]),
                ),
            )
        )

    for spec in data["providers"]:
        registry.register_provider(_rebuild_provider(spec, fields, registry))

    return registry
