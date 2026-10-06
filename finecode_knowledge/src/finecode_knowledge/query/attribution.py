"""Which buckets could have answered a footprint key (R11, Phase 4).

A reservation must be raised whenever a query's answer **could** have been
affected by an unconfirmed bucket -- including when the query read nothing.

That last clause is the whole design. ``q.not_(declares_dep(owner, missing))``
succeeds precisely when a scan comes back **empty**, so attributing by the facts
actually returned reports a clean verdict over a stale input: the rule's "no
violation" rests on a bucket that may since have grown the very fact that would
have made it a violation. Same failure direction as ADR-0013 D4.3, one layer up,
and silent in the same way.

So attribution is **static, from the schema, never from the rows**. A key names
a slot; ``SUPPLIES_FIELDS`` / ``SUPPLIES_EDGES`` say which providers may write
into that slot; those providers' buckets are what the key depends on. The set is
a sound superset by construction and costs one pass over the providers per
distinct key.

Over-approximating costs a spurious reservation. Under-approximating costs a
clean verdict over a stale answer. Only the first is acceptable, and the
asymmetry is why this module never consults the store.
"""

from __future__ import annotations

import typing

from finecode_knowledge.query.footprint import AccessKey

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = ["providers_for_footprint", "providers_for_key"]


def providers_for_key(key: AccessKey, schema: SchemaRegistry) -> set[str]:
    """Every provider whose ``SUPPLIES`` could put a fact inside the slot *key* names."""
    tag = key[0]
    if tag == "field":
        _, entity_type, field, _entity, _value = key
        return {
            provider.qualified_id()
            for provider in schema.providers()
            if any(
                f.entity == entity_type and f.qualified_name == field
                for f in provider.SUPPLIES_FIELDS
            )
        }
    if tag == "edge":
        kind = key[1]
        return {
            provider.qualified_id()
            for provider in schema.providers()
            if any(r.qualified_name == kind for r in provider.SUPPLIES_EDGES)
        }
    if tag == "entity":
        return _suppliers_of_type(key[1].type, schema)
    if tag == "type":
        return _suppliers_of_type(key[1], schema)
    return set()


def _suppliers_of_type(entity_type: str, schema: SchemaRegistry) -> set[str]:
    """Every provider supplying *any* field of *entity_type*.

    ``entities_of_type`` and ``contains`` ask whether an entity exists at all,
    and an entity exists exactly when some provider has asserted some field
    about it -- so any supplier of any field of the type can change the answer.
    Narrowing this to the fields the query went on to read would be unsound for
    the same reason the module exists.
    """
    return {
        provider.qualified_id()
        for provider in schema.providers()
        if any(f.entity == entity_type for f in provider.SUPPLIES_FIELDS)
    }


def providers_for_footprint(
    keys: typing.Iterable[AccessKey], schema: SchemaRegistry
) -> set[str]:
    providers: set[str] = set()
    for key in keys:
        providers |= providers_for_key(key, schema)
    return providers
