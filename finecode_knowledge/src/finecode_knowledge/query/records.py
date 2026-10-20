"""Reading a whole entity's record through the owner (R21, Phase 1b).

**The one read that is not a query, and why that is not a gap.** A query names
predicates: ``BookFields.isbn(book, isbn)`` asks about *that* field. A projection
rendering "everything known about this entity" cannot name its fields, because
the set is open -- ADR-0017 D7 and R18/R19 exist so a third party can declare a
field on somebody else's entity type, and a projection that enumerated the fields
it knew about would drop exactly those. So the access is
``entity -> {field: value with provenance}``, quantified over field *names*,
which conjunctive Datalog has no way to say.

That leaves two options and only one of them is honest. Handing the consumer a
``FactSource`` is what the code did before this module existed, and it breaks
R21: a read outside the channel contributes **no footprint key**, so any memoized
answer resting on it is never invalidated -- ADR-0013 D4.3's silent false
negative, one layer up. So the read stays outside the *query* channel and inside
the *read* channel: it goes to whoever owns the store, and it is recorded in the
footprint exactly like a scan.

## The footprint key is already defined

``footprint.entity_key(ref)`` and ``attribution._suppliers_of_type`` were both
built for this shape and neither needed changing: an ``("entity", ref)`` key is
invalidated by any fact about *ref*, and it attributes to every provider
supplying any field of that entity's type. Over-approximating, in the one
direction that is safe.

## Bounded by construction

``records`` takes a **sequence** of refs and answers in one call. That is what
keeps a projection's cost independent of its result size: a ``which_handlers``
over forty handlers costs one record fetch, not forty. The per-ref shape was
rejected for the same reason ADR-0013 D1 rejected a per-literal backend
protocol -- it turns one question into N round trips, and that count is the
whole point.
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.fact_source import Conflict, FieldValue, Record
from finecode_knowledge.model.wire import (
    prov_from_json,
    prov_to_json,
    ref_from_json,
    ref_to_json,
)
from finecode_knowledge.query.ir_wire import QueryWireError

if typing.TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "RECORDS_WIRE_VERSION",
    "RecordSource",
    "records_from_json",
    "records_to_json",
    "refs_from_json",
    "refs_to_json",
]

RECORDS_WIRE_VERSION = 1


class RecordSource(typing.Protocol):
    """Whatever can answer "everything known about these entities".

    Deliberately **not** folded into ``Backend``. ``Backend`` is the query
    boundary (ADR-0016 D2) and its one method takes a whole ``Query``; a record
    fetch is a different question with a different shape, and giving the query
    protocol a second method would make every backend -- including a future graph
    engine's -- owe an implementation of something it has no query to compile.

    Both shipped backends satisfy both protocols, so a consumer still threads one
    object. The split is about what each protocol *promises*, not about how many
    objects exist.
    """

    async def records(self, refs: Sequence[EntityRef]) -> tuple[Record, ...]:
        """One ``Record`` per ref, in the order asked.

        An entity with no facts yields an empty ``Record`` rather than being
        omitted: dropping it would make the result's length depend on the store's
        contents, and every caller would have to re-zip by hand.
        """
        ...


# ---- the wire form -----------------------------------------------------


def refs_to_json(refs: Sequence[EntityRef]) -> list[dict]:
    return [ref_to_json(ref) for ref in refs]


def refs_from_json(data: list[dict]) -> tuple[EntityRef, ...]:
    return tuple(ref_from_json(item) for item in data)


def _field_value_to_json(field_value: FieldValue) -> dict:
    return {"v": field_value.value, "prov": prov_to_json(field_value.prov)}


def _field_value_from_json(data: dict) -> FieldValue:
    return FieldValue(value=data["v"], prov=prov_from_json(data["prov"]))


def _conflict_to_json(conflict: Conflict) -> dict:
    return {
        "entity": ref_to_json(conflict.entity),
        "field": conflict.field,
        "values": [_field_value_to_json(v) for v in conflict.values],
    }


def _conflict_from_json(data: dict) -> Conflict:
    return Conflict(
        entity=ref_from_json(data["entity"]),
        field=data["field"],
        values=tuple(_field_value_from_json(v) for v in data["values"]),
    )


def records_to_json(records: Sequence[Record]) -> dict:
    """Records **and** their conflicts.

    A conflict is an input this answer could not confirm (ADR-0014 D5), so
    dropping it on the wire would let a projection render a contested field as
    settled -- the silent last-write-wins C9 excludes, reintroduced by a
    serializer.
    """
    return {
        "v": RECORDS_WIRE_VERSION,
        "records": [
            {
                "fields": {
                    name: _field_value_to_json(value)
                    for name, value in record.fields.items()
                },
                "conflicts": [_conflict_to_json(c) for c in record.conflicts],
            }
            for record in records
        ],
    }


def records_from_json(data: dict) -> tuple[Record, ...]:
    """Rebuild records from *data*.

    Raises:
        QueryWireError: *data* is not a record payload this version can read.
    """
    version = data.get("v")
    if version != RECORDS_WIRE_VERSION:
        raise QueryWireError(
            f"Unsupported serialized-records version {version!r}; this build reads "
            f"version {RECORDS_WIRE_VERSION}."
        )
    return tuple(
        Record(
            fields={
                name: _field_value_from_json(value)
                for name, value in item["fields"].items()
            },
            conflicts=tuple(_conflict_from_json(c) for c in item["conflicts"]),
        )
        for item in data["records"]
    )
