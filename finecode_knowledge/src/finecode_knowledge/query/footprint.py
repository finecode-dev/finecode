"""Access keys and the footprint collector (ADR-0013 D4/D5).

**The access key is the index key is the footprint key.** These were framed as
two work items and they are one table: the index exists to *serve* an access
pattern, the footprint exists to *name* one, and once the pattern is a canonical
tuple both are the same thing.

Three properties make it work, and all three are load-bearing.

**1. The key is the tightest sound dependency for that call.** A both-bound
``edge_facts(k, src=s, dst=d)`` depends only on whether that one fact exists;
adding ``(k, s, d')`` cannot change its answer. Precision comes from the *call*,
so the store's indexing strategy is free to be coarser without affecting it.

**2. A changed fact matches a recorded key without consulting the store.**
``matches`` is wildcard comparison over a tuple, O(1) per key. An opaque hash of
a result set would not have this property, and the memo layer would have to
re-execute to learn what changed.

**3. The key is recorded per *access*, never per *returned row*.** This is the
property that makes negation sound, and it is the trap laziness would otherwise
set. ``q.not_(declares_dep(owner, missing))`` succeeds precisely when a scan
comes back **empty**. If the footprint were the rows returned, an empty scan
would record nothing, a fact added later would match no key, and the rule's
cached "no violation" would never be invalidated -- a silent false negative, in
a system whose entire purpose is to report violations.

The same property makes early termination safe: a scan abandoned after one
element consulted the same slot as one drained fully. Over-approximation costs a
spurious recomputation; under-approximation costs a wrong answer, and the key is
defined so only the first is possible.
"""

from __future__ import annotations

import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.facts import EdgeFact, Emission, FieldFact

__all__ = [
    "AccessKey",
    "FootprintCollector",
    "edge_key",
    "entity_key",
    "field_key",
    "matches",
    "type_key",
]

AccessKey: typing.TypeAlias = tuple[object, ...]
"""A canonical tuple naming one access. Comparable, hashable, renderable."""


def edge_key(
    kind: str, src: EntityRef | None = None, dst: EntityRef | None = None
) -> AccessKey:
    return ("edge", kind, src, dst)


def field_key(
    entity_type: str,
    field: str,
    entity: EntityRef | None = None,
    value: object | None = None,
) -> AccessKey:
    return ("field", entity_type, field, entity, value)


def entity_key(ref: EntityRef) -> AccessKey:
    return ("entity", ref)


def type_key(entity_type: str) -> AccessKey:
    return ("type", entity_type)


def matches(fact: Emission, key: AccessKey) -> bool:
    """Whether *fact* falls inside the slot *key* names (ADR-0013 D4.2).

    Wildcard comparison: ``None`` in a key position matches anything. An
    ``EdgeFact(k, s, d)`` invalidates ``("edge", k', s', d')`` iff ``k' == k``
    and ``s' in (None, s)`` and ``d' in (None, d)``.
    """
    tag = key[0]
    if tag == "edge":
        if not isinstance(fact, EdgeFact):
            return False
        _, kind, src, dst = key
        return fact.kind == kind and _wild(src, fact.src) and _wild(dst, fact.dst)
    if tag == "field":
        if not isinstance(fact, FieldFact):
            return False
        _, entity_type, field, entity, value = key
        return (
            fact.entity.type == entity_type
            and fact.field == field
            and _wild(entity, fact.entity)
            and _wild(value, fact.value)
        )
    if tag == "entity":
        ref = key[1]
        return _fact_entity(fact) == ref
    if tag == "type":
        entity_type = key[1]
        held = _fact_entity(fact)
        return held is not None and held.type == entity_type
    return False


def _wild(bound: object, actual: object) -> bool:
    return bound is None or bound == actual


def _fact_entity(fact: Emission) -> EntityRef | None:
    return fact.entity if isinstance(fact, FieldFact) else None


class FootprintCollector:
    """The set of slots one execution consulted.

    Owned by the **interpreter**, not by the ``FactSource`` (ADR-0013 D5). The
    interpreter already constructs each call's argument pattern in order to make
    the call, so appending the key costs one tuple per access and cannot drift
    from what was actually asked. Putting recording inside the source instead
    would make freshness correctness depend on every backend author remembering
    to instrument every method, and would duplicate key construction on both
    sides of a call.

    A first-class object deliberately, so "read count is independent of result
    size" is asserted against ``len(collector)`` directly rather than
    reconstructed by instrumenting something.
    """

    __slots__ = ("_keys",)

    def __init__(self) -> None:
        self._keys: dict[AccessKey, None] = {}

    def record(self, key: AccessKey) -> None:
        self._keys.setdefault(key, None)

    @property
    def keys(self) -> tuple[AccessKey, ...]:
        return tuple(self._keys)

    def invalidated_by(self, fact: Emission) -> tuple[AccessKey, ...]:
        """Every recorded key *fact* would change the answer of."""
        return tuple(key for key in self._keys if matches(fact, key))

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: AccessKey) -> bool:
        return key in self._keys

    def __repr__(self) -> str:
        return f"FootprintCollector({len(self._keys)} keys)"
