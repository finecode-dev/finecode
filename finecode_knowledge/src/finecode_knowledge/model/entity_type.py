from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.naming import declaring_package

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.fields import AnyField
    from finecode_knowledge.model.literal import Literal

__all__ = ["EntityRef", "EntityType"]


@dataclasses.dataclass(frozen=True)
class EntityRef:
    type: str
    """The entity type's **qualified** name (``"fine_knowledge.Action"``).

    Refs are constructed through the entity class, which knows its own
    qualifier, so this is never spelled by hand at a call site (ADR-0017 D6).
    Storing it qualified is what keeps a stored fact permanently unambiguous:
    resolving at read instead would leave every previously-stored fact
    unresolvable the day a second extension declares the same local name."""
    key: tuple[str, ...]


class EntityType:
    NAME: typing.ClassVar[str]
    """The **local** name. The declaring package supplies the qualifier."""
    KEY: typing.ClassVar[list[AnyField]]
    CORE: typing.ClassVar[list[AnyField]]

    @classmethod
    def qualified_name(cls) -> str:
        """``<declaring package>.<NAME>`` -- what ``EntityRef.type`` carries.

        Derived from ``cls.__module__`` by the same rule the registry uses
        (ADR-0017 D3), so a ref is qualified from construction and does not
        have to consult a registry to be spelled correctly.
        """
        return f"{declaring_package(cls)}.{cls.NAME}"

    @classmethod
    def key(cls, entity: object, **key_values: object) -> Literal:
        """A literal addressing *entity* by any subset of its ``KEY`` fields (ADR-0019 D1).

        ``Preset.key(included, source=missing)`` says *which* preset is meant. It
        does **not** say the store knows anything about it -- a rule that needs
        the entity to be known says so with an edge or field literal, and the
        explicit spelling is what makes that reviewable in the rule text rather
        than by consulting ``schema.py`` (D3).

        The key value is already present, unconditionally, in every reference:
        ``ref()`` below builds the key *from* the KEY field values (C2), so
        projecting one back out cannot disagree with what a provider asserted.
        That is why this reads nothing and records no footprint key.

        Contrast ``PresetFields.source(p, v)``, which stays a fact scan with
        provenance and a footprint key, and now means "presets that were
        actually scanned".

        Raises:
            SchemaError: a named field is not part of this type's ``KEY``.
        """
        from finecode_knowledge.model.literal import Literal, LiteralKind

        key_ids = [f.id for f in cls.KEY]
        if "at" in key_values and "at" not in key_ids:
            raise SchemaError(
                f"{cls.NAME}.key() cannot bind provenance. Addressing an entity reads no "
                "fact, so there is no provenance to bind (ADR-0019 D4). Use the field "
                "literal if you need the provenance of an assertion."
            )
        unknown = sorted(set(key_values) - set(key_ids))
        if unknown:
            raise SchemaError(
                f"{cls.NAME}.key(): {unknown} is not part of its KEY ({key_ids}). "
                f"To read an asserted field use the field literal instead."
            )
        named = tuple(name for name in key_ids if name in key_values)
        return Literal(
            kind=LiteralKind.KEY,
            predicate=cls.qualified_name(),
            entity_type=cls.qualified_name(),
            key_fields=named,
            terms=(entity, *(key_values[name] for name in named)),
        )

    @classmethod
    def known(cls, entity: object) -> Literal:
        """A literal holding when the store has **any** fact about *entity*.

        The existence question ``key()`` deliberately does not ask and a field
        literal can only ask about one named field at a time. ``Action.known(a)``
        conjoined with ``Action.key(a, source=text)`` is "an action that was
        actually scanned and is spelled this way" -- which is what a resolver
        means, and what it previously had to leave the query language to say.

        *entity* must be **bound** by the time this literal runs: it filters, it
        does not enumerate. Binding an entity from it would mean iterating every
        reference the store has ever seen, which is ADR-0019 D6's refusal for the
        same reason.
        """
        from finecode_knowledge.model.literal import Literal, LiteralKind

        return Literal(
            kind=LiteralKind.KNOWN,
            predicate=cls.qualified_name(),
            entity_type=cls.qualified_name(),
            terms=(entity,),
        )

    @classmethod
    def ref(cls, **key_values: object) -> EntityRef:
        key_ids = [f.id for f in cls.KEY]
        unknown = set(key_values) - set(key_ids)
        if unknown:
            raise SchemaError(f"{cls.NAME}: unknown key field(s) {sorted(unknown)}")
        missing = set(key_ids) - set(key_values)
        if missing:
            raise SchemaError(f"{cls.NAME}: missing key field(s) {sorted(missing)}")
        return EntityRef(
            type=cls.qualified_name(), key=tuple(str(key_values[k]) for k in key_ids)
        )
