from __future__ import annotations

import dataclasses
import hashlib
import json
import typing

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import (
    BandViolationError,
    SchemaError,
    SuppliesViolationError,
)
from finecode_knowledge.model.fact_source import Conflict, FieldValue, Record, Revision
from finecode_knowledge.model.facts import EdgeFact, Emission, FieldFact, RunStamp
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.model.wire import fact_from_json, fact_to_json

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = ["FactStore", "FieldValue"]

DEFAULT_UNIT_ID = "__all__"
"""Bucket unit for a provider that has not been broken down by source unit yet
(T3). Ingesting under this default replaces everything the provider has ever
reported, matching the pre-T3 whole-provider-bucket behavior -- the shim that
keeps every existing two-argument ``ingest(provider_id, emissions)`` call
working unchanged while the seam for real per-unit buckets exists for
providers that opt in via ``unit_id=``."""


@dataclasses.dataclass
class _Indexes:
    """The seven indexes of ADR-0013 D4, plus ADR-0014 D4's contested set.

    Each one exists to serve exactly one bound-argument pattern of
    ``edge_facts`` / ``field_facts`` -- which is the same tuple the interpreter
    records as its footprint key. The index and the footprint were framed as two
    work items; they are one table, because the index exists to *serve* an access
    pattern and the footprint exists to *name* one.
    """

    by_kind: dict[str, list[EdgeFact]] = dataclasses.field(default_factory=dict)
    by_kind_src: dict[tuple[str, EntityRef], list[EdgeFact]] = dataclasses.field(
        default_factory=dict
    )
    by_kind_dst: dict[tuple[str, EntityRef], list[EdgeFact]] = dataclasses.field(
        default_factory=dict
    )
    by_entity: dict[EntityRef, list[FieldFact]] = dataclasses.field(
        default_factory=dict
    )
    by_type_field: dict[tuple[str, str], list[FieldFact]] = dataclasses.field(
        default_factory=dict
    )
    by_type_field_value: dict[tuple[str, str, object], list[FieldFact]] = (
        dataclasses.field(default_factory=dict)
    )
    by_type: dict[str, list[EntityRef]] = dataclasses.field(default_factory=dict)
    contested: dict[tuple[str, str, EntityRef], Conflict] = dataclasses.field(
        default_factory=dict
    )
    """``(entity_type, field, entity)`` slots on which providers disagree (C9).

    Detected here, at ingest, rather than from the scan the interpreter already
    walks -- which was the cheaper design and is **incomplete in the unsound
    direction**. A value-bound literal such as ``PackageFields.name(pkg, missing)``
    scans only facts whose value is ``missing``, so a provider asserting a
    *different* value for the same slot is not in that scan and its conflict is
    invisible. That yields a result claiming ``verified`` over a contested input.
    Ingest-side detection sees every fact by construction (ADR-0014 D4)."""


class FactStore:
    def __init__(
        self, schema: SchemaRegistry, *, revision: Revision | None = None
    ) -> None:
        self._schema = schema
        self._buckets: dict[tuple[str, str], list[Emission]] = {}
        self._units: dict[tuple[str, str], Unit] = {}
        self._indexes: _Indexes | None = None
        self._pinned_revision = revision
        self._revision: Revision | None = revision

    # ---- write path ---------------------------------------------------

    def ingest(
        self,
        provider_id: str,
        emissions: typing.Iterable[Emission],
        *,
        unit_id: str = DEFAULT_UNIT_ID,
        unit: Unit | None = None,
    ) -> int:
        """Replace *(provider_id, unit_id)*'s bucket with its exact-unique emissions, returning how many were stored.

        *unit_id* is the provider's captured footprint -- typically a single
        source path -- so re-ingesting one unit leaves every other unit's
        facts (including other units from the same provider) untouched. A
        provider not yet broken down by unit ingests everything under
        ``DEFAULT_UNIT_ID``, which replaces the whole provider as before (T3).

        *unit* is the production path (R6/R11): it carries the same id **plus**
        the declared inputs whose fingerprints let a later cold start confirm
        this bucket. Passing only *unit_id* records a unit that declares no
        inputs, which is honest rather than convenient -- a bucket with nothing
        to check cannot be confirmed, so it reserves. That is why the argument
        is optional and its absence is safe: forgetting it costs a reservation,
        never a false clean verdict.

        Exact duplicates are collapsed in first-occurrence order. Emissions differing
        in any field -- including two values for one entity field -- stay distinct, so
        conflicts still surface at ``record()`` and ``conflicts()``.

        Field names and edge kinds are normalized to their **qualified** form here
        and stored qualified (ADR-0017 D6), resolved against the provider's own
        ``SUPPLIES`` declarations -- which hold the registered objects, so the
        resolution is exact and needs no unqualified registry lookup. Entity ref
        types arrive qualified already, from ``EntityType.ref()``. Resolving at
        read instead would leave stored facts ambiguous the day a second extension
        declares the same local name, with no record of which package meant it.

        Raises:
            SchemaError: *provider_id* is unregistered, an emission addresses an
                unknown entity type or a key of the wrong arity, or *unit*
                disagrees with *provider_id* / *unit_id*.
            BandViolationError: an emission claims the DERIVED band.
            SuppliesViolationError: an emission names a field or edge kind the provider
                does not declare in SUPPLIES.
        """
        if unit is not None:
            if unit.provider_id != provider_id:
                raise SchemaError(
                    f"Unit belongs to {unit.provider_id!r}, ingested under {provider_id!r}. "
                    "The bucket key would name one provider and its inputs another."
                )
            if unit_id != DEFAULT_UNIT_ID and unit_id != unit.unit_id:
                raise SchemaError(
                    f"Conflicting unit ids: unit_id={unit_id!r} but unit.unit_id="
                    f"{unit.unit_id!r}. Pass one or the other."
                )
            unit_id = unit.unit_id

        provider = self._schema.provider(provider_id)
        supplied_fields: dict[tuple[str, str], str] = {}
        for field in provider.SUPPLIES_FIELDS:
            qualified = field.qualified_name
            supplied_fields[(field.entity, field.id)] = qualified
            supplied_fields[(field.entity, qualified)] = qualified
        supplied_edges: dict[str, str] = {}
        for rel in provider.SUPPLIES_EDGES:
            supplied_edges[rel.kind] = rel.qualified_name
            supplied_edges[rel.qualified_name] = rel.qualified_name

        normalized: list[Emission] = []
        for emission in emissions:
            if emission.prov.band is Band.DERIVED:
                raise BandViolationError("DERIVED-band facts are never stored")
            if isinstance(emission, FieldFact):
                self._validate_ref(emission.entity)
                qualified_field = supplied_fields.get(
                    (emission.entity.type, emission.field)
                )
                if qualified_field is None:
                    raise SuppliesViolationError(
                        f"{provider_id} does not supply {emission.entity.type}.{emission.field}"
                    )
                normalized.append(dataclasses.replace(emission, field=qualified_field))
            else:
                self._validate_ref(emission.src)
                self._validate_ref(emission.dst)
                qualified_kind = supplied_edges.get(emission.kind)
                if qualified_kind is None:
                    raise SuppliesViolationError(
                        f"{provider_id} does not supply edge kind {emission.kind!r}"
                    )
                normalized.append(dataclasses.replace(emission, kind=qualified_kind))

        stored = list(dict.fromkeys(normalized))
        self._buckets[(provider_id, unit_id)] = stored
        self._units[(provider_id, unit_id)] = unit or Unit(
            provider_id=provider_id, unit_id=unit_id
        )
        self._invalidate()
        return len(stored)

    def retract(self, provider_id: str, unit_id: str) -> int:
        """Drop *(provider_id, unit_id)*'s bucket entirely, returning how many facts it held.

        ``goals.md`` §4.7 Q8: a provider asked about a unit and enumerating
        nothing for it *is* the retraction -- the unit no longer exists, so its
        own facts go. **Never cascades.** An
        ``EdgeFact`` some other unit owns that names a ref this bucket used to
        supply is left exactly as it was: dangling rather than followed and
        deleted. Q8 settled why -- a dangling edge is a legal queryable state in
        a federated, incrementally-built store (its endpoint's provider may
        simply not have run yet), so walking from a retracted unit to every edge
        that mentions its refs would delete some that are only *pending*, and
        would make retraction cost a graph traversal rather than O(unit).
        Whether a given dangling edge is a genuine orphan is a question for a
        rule over the graph, not a store invariant.

        A no-op returning 0 for a bucket the store never held -- retracting
        something already absent is not an error, matching ``ingest``'s own
        replace-in-place semantics.
        """
        key = (provider_id, unit_id)
        removed = self._buckets.pop(key, None)
        self._units.pop(key, None)
        if removed is None:
            return 0
        self._invalidate()
        return len(removed)

    @property
    def schema(self) -> SchemaRegistry:
        """The registry this store validates against.

        Exposed because building a ``Unit`` needs it: a provider resolves its
        supplied fields and edges to their declaring modules through the
        registry (ADR-0026 D1), and the caller that ingests already holds the
        store rather than the registry."""
        return self._schema

    def unit(self, provider_id: str, unit_id: str) -> Unit | None:
        """The declared inputs of one bucket, or ``None`` if it holds no facts."""
        return self._units.get((provider_id, unit_id))

    def bucket(self, provider_id: str, unit_id: str) -> tuple[Emission, ...]:
        """Exactly the facts one ``(provider, unit)`` bucket holds.

        The memo layer's output-side cutoff needs a bucket's facts *as a set* to
        digest them (``goals.md`` §4.4), and no read on the ``FactSource`` seam
        answers that: every one of those is an access *pattern* -- a kind, a
        slot, a type -- because that is what a query asks and what a footprint
        key names. A bucket is an ownership unit, not a pattern.

        Returned as a tuple, so a caller cannot mutate a bucket by holding the
        list the store is still using.
        """
        return tuple(self._buckets.get((provider_id, unit_id), ()))

    def units(self) -> Iterator[Unit]:
        return iter(self._units.values())

    def _validate_ref(self, ref: EntityRef) -> None:
        entity_type = self._schema.entity_type(ref.type)
        if len(ref.key) != len(entity_type.KEY):
            raise SchemaError(f"{ref.type}: wrong key arity for {ref.key!r}")

    def _invalidate(self) -> None:
        """Drop the derived structures; the next read rebuilds them.

        The invalidation *event* is a ``(provider, unit)`` bucket ingest, as
        ADR-0013 D4 requires. Rebuilding whole rather than patching the changed
        bucket in place is a deliberate simplification matched to the real access
        pattern: extraction ingests many buckets and reads none, then execution
        reads many times and ingests nothing, so a lazy rebuild costs exactly one
        pass before the first query rather than one per bucket.
        """
        self._indexes = None
        if self._pinned_revision is None:
            self._revision = None

    # ---- indexes ------------------------------------------------------

    @property
    def _idx(self) -> _Indexes:
        if self._indexes is None:
            self._indexes = self._build_indexes()
        return self._indexes

    def _build_indexes(self) -> _Indexes:
        idx = _Indexes()
        seen_by_type: dict[str, dict[EntityRef, None]] = {}
        slots: dict[tuple[str, str, EntityRef], dict[object, FieldValue]] = {}

        for emissions in self._buckets.values():
            for emission in emissions:
                if isinstance(emission, EdgeFact):
                    idx.by_kind.setdefault(emission.kind, []).append(emission)
                    idx.by_kind_src.setdefault(
                        (emission.kind, emission.src), []
                    ).append(emission)
                    idx.by_kind_dst.setdefault(
                        (emission.kind, emission.dst), []
                    ).append(emission)
                    continue

                entity_type = emission.entity.type
                idx.by_entity.setdefault(emission.entity, []).append(emission)
                idx.by_type_field.setdefault((entity_type, emission.field), []).append(
                    emission
                )
                idx.by_type_field_value.setdefault(
                    (entity_type, emission.field, emission.value), []
                ).append(emission)
                seen_by_type.setdefault(entity_type, {}).setdefault(
                    emission.entity, None
                )
                slots.setdefault(
                    (entity_type, emission.field, emission.entity), {}
                ).setdefault(
                    emission.value, FieldValue(value=emission.value, prov=emission.prov)
                )

        idx.by_type = {name: list(refs) for name, refs in seen_by_type.items()}
        idx.contested = {
            slot: Conflict(entity=slot[2], field=slot[1], values=tuple(values.values()))
            for slot, values in slots.items()
            if len(values) > 1
        }
        return idx

    # ---- FactSource ---------------------------------------------------

    @property
    def revision(self) -> Revision:
        """A content digest over exactly the facts served (ADR-0013 D6.2).

        Derived from the stored facts rather than from the file, so a store
        assembled in memory has one too; ``from_json`` accepts the fact file's
        own digest to pin instead.
        """
        if self._revision is None:
            digest = hashlib.sha256()
            wire = sorted(
                json.dumps(fact_to_json(f), sort_keys=True, default=str)
                for emissions in self._buckets.values()
                for f in emissions
            )
            for line in wire:
                digest.update(line.encode())
                digest.update(b"\n")
            self._revision = Revision(digest.hexdigest())
        return self._revision

    def edge_facts(
        self, kind: str, *, src: EntityRef | None = None, dst: EntityRef | None = None
    ) -> Iterator[EdgeFact]:
        """Every stored edge of *kind*, optionally pinned at either end.

        Lazy by contract, not by accident: an ``any(...)`` that abandons the
        scan after one element consults the same index slot as one drained
        fully, so early termination and full drain record the identical
        footprint key.
        """
        idx = self._idx
        if src is not None and dst is not None:
            candidates = idx.by_kind_src.get((kind, src), ())
            return (e for e in candidates if e.dst == dst)
        if src is not None:
            return iter(idx.by_kind_src.get((kind, src), ()))
        if dst is not None:
            return iter(idx.by_kind_dst.get((kind, dst), ()))
        return iter(idx.by_kind.get(kind, ()))

    def field_facts(
        self,
        entity_type: str,
        field: str,
        *,
        entity: EntityRef | None = None,
        value: object | None = None,
    ) -> Iterator[FieldFact]:
        """Every stored fact for the ``(entity_type, field)`` slot, optionally narrowed.

        The slot is a *pair*: ``name`` exists on ``Package``, ``Handler`` and
        ``Environment``, so the field name alone does not identify it.
        """
        idx = self._idx
        if entity is not None:
            candidates: typing.Iterable[FieldFact] = (
                f
                for f in idx.by_entity.get(entity, ())
                if f.field == field and f.entity.type == entity_type
            )
            if value is not None:
                candidates = (f for f in candidates if f.value == value)
            return iter(candidates)
        if value is not None:
            return iter(idx.by_type_field_value.get((entity_type, field, value), ()))
        return iter(idx.by_type_field.get((entity_type, field), ()))

    def conflicts(
        self, entity_type: str, field: str, *, entity: EntityRef | None = None
    ) -> Iterator[Conflict]:
        """Slots of ``(entity_type, field)`` on which providers disagree (ADR-0014 D4)."""
        idx = self._idx
        if entity is not None:
            found = idx.contested.get((entity_type, field, entity))
            return iter(() if found is None else (found,))
        return (
            conflict
            for (slot_type, slot_field, _), conflict in idx.contested.items()
            if slot_type == entity_type and slot_field == field
        )

    def record(self, ref: EntityRef) -> Record:
        """Every field asserted about *ref*, plus whatever about it is contested.

        A contested slot contributes its *first-seen* value to ``fields`` and the
        whole disagreement to ``conflicts``. It does not raise: an audit whose
        entire purpose is to report violations must not lose its whole result to
        one contested field somewhere in the touched set (ADR-0014 D5).
        """
        idx = self._idx
        fields: dict[str, FieldValue] = {}
        for fact in idx.by_entity.get(ref, ()):
            fields.setdefault(fact.field, FieldValue(value=fact.value, prov=fact.prov))
        found = tuple(
            conflict
            for (_, _, slot_entity), conflict in idx.contested.items()
            if slot_entity == ref
        )
        return Record(fields=fields, conflicts=found)

    def contains(self, entity_type: str, ref: EntityRef) -> bool:
        """Whether *ref* has any facts at all -- the one existence question the scans cannot answer."""
        return ref.type == entity_type and bool(self._idx.by_entity.get(ref))

    def entities_of_type(self, entity_type: str) -> Iterator[EntityRef]:
        return iter(self._idx.by_type.get(entity_type, ()))

    # ---- serialization ------------------------------------------------

    def to_json(self) -> dict:
        all_facts: list[Emission] = []
        runs: dict[RunStamp, None] = {}
        for emissions in self._buckets.values():
            for emission in emissions:
                all_facts.append(emission)
                runs.setdefault(emission.prov.run, None)
        return {
            "facts": [fact_to_json(f) for f in all_facts],
            "buckets": [
                {
                    "provider": provider_id,
                    "unit": unit_id,
                    "count": len(emissions),
                    # R11: the fingerprints ride with the facts, because a cold
                    # start has nothing else to compare against (§4.8).
                    **self._units[(provider_id, unit_id)].to_json(),
                }
                for (provider_id, unit_id), emissions in self._buckets.items()
            ],
            "runs": [{"id": r.id, "at": r.observed_at} for r in runs],
        }

    @classmethod
    def from_json(
        cls, schema: SchemaRegistry, data: dict, *, revision: Revision | None = None
    ) -> FactStore:
        """Rebuild a store from its wire form.

        *revision* pins the fact file's own content digest -- the file-loaded
        path's answer to D6.2. Omitted, the store derives one from the facts it
        holds, which identifies the same content by a different route.
        """
        store = cls(schema, revision=revision)
        facts = [fact_from_json(f) for f in data["facts"]]
        offset = 0
        for bucket in data["buckets"]:
            count = bucket["count"]
            key = (bucket["provider"], bucket["unit"])
            store._buckets[key] = facts[offset : offset + count]
            store._units[key] = Unit.from_json(
                bucket["provider"], bucket["unit"], bucket
            )
            offset += count
        return store
