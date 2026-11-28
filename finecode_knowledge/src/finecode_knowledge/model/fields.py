from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Literal, LiteralKind, Term

__all__ = ["AnyField", "Field"]

E = typing.TypeVar("E")
T = typing.TypeVar("T")


@dataclasses.dataclass(frozen=True)
class Field(typing.Generic[E, T]):
    """A field of entity type *E* carrying values of type *T*.

    Both parameters are phantom -- the runtime value only carries ``entity`` as
    a string and no attribute holds a ``T`` -- so erasure costs nothing. They
    exist to give a field literal's *entity* term the same call-site checking
    its *value* term already had (ADR-0005).

    Declare fields in **annotation position**, never by subscripting the
    constructor::

        class PackageFields:
            name: Field[Package, str] = Field("name", entity="Package")

    A ``*Fields`` class is declared before the entity class that lists it in
    ``KEY``/``CORE``, so naming the entity class in value position would invert
    a real import/declaration cycle. Under ``from __future__ import
    annotations`` the subscript is never evaluated at runtime, which is exactly
    what lets the forward reference resolve for a type checker while leaving
    declaration order untouched.

    That is how a *declared* schema spells it. Constructing a field ad hoc --
    ``Field("name", entity="Widget")``, no ``*Fields`` class and no entity class
    anywhere -- stays first-class: this layer is schema-agnostic, and an entity
    type it has never seen (a federated one, or one a test declares) has no
    class to annotate against. The ``entity`` string, not the annotation, is
    what the runtime reads.

    So the two can drift -- no type checker can see that ``"Package"`` and
    ``Package`` are the same type. ``tests/test_schema_typing.py`` closes that by
    reflection over every declared field and is a required part of this design,
    not an optional extra. See ADR-0005 for why the duplication is kept.

    **The author writes the local name; the registry supplies the qualifier**
    (ADR-0017 D3). ``package`` is empty until registration, at which point
    ``SchemaRegistry`` binds it to the top-level package of the declaring
    ``*Fields`` class's module and rewrites ``entity`` to its qualified form --
    a local ``entity="Package"`` becomes ``"fine_knowledge.Package"`` when
    ``PackageFields`` is declared in ``fine_knowledge``. A field naming an
    entity another package owns spells that entity qualified from the start
    (``entity="fine_knowledge.Package"``), which is left as written. Everything
    downstream of registration -- the store's keys, emitted facts, ``__str__``
    -- therefore reads qualified names only, and ``my_ext`` cannot claim
    ``fine_knowledge.`` for its own field by typing it, because it does not
    type it (ADR-0017 D4).
    """

    id: str
    entity: str
    package: str | None = dataclasses.field(default=None, compare=False)
    """The declaring package, bound at registration. Excluded from identity:
    the package is part of the registry *key*, so two registrations of one
    field object (``CORE`` overlap, module re-import) still compare equal and
    hit D5's no-op clause."""

    @property
    def qualified_name(self) -> str:
        """``<declaring package>.<id>`` -- how the store and every fact spell this field."""
        if self.package is None:
            raise SchemaError(
                f"{self.entity}.{self.id} has no qualified name: it was never registered. "
                "Register its declaring class with `register_namespace`, or the field "
                "itself with `register_field(f, package=...)`."
            )
        return f"{self.package}.{self.id}"

    def _bind(self, package: str, entity: str) -> None:
        """Attach the derived qualifier (ADR-0017 D3). Called only by ``SchemaRegistry``."""
        if self.package is not None and self.package != package:
            raise SchemaError(
                f"Field {self.entity}.{self.id} is already declared by {self.package!r}; "
                f"{package!r} cannot re-declare the same object. Declare a separate "
                "Field instance in the second package."
            )
        object.__setattr__(self, "package", package)
        object.__setattr__(self, "entity", entity)

    def __call__(self, entity: Term, value: Term, *, at: Term | None = None) -> Literal:
        """A field literal: *entity* has this field with *value* (FR4).

        Spelled identically to an edge literal, deliberately -- ``FR4``'s whole
        content is that a rule author writes one thing. ``at=`` binds the
        fact's provenance as an ordinary term (FR5).

        A constant is a legal term: ``PackageFields.name(pkg, "fine_lint")``.
        """
        return Literal(
            kind=LiteralKind.FIELD,
            predicate=self.qualified_name,
            terms=(entity, value),
            entity_type=self.entity,
            at=at,
        )

    def __str__(self) -> str:
        return f"{self.entity}.{self.id}"


AnyField: typing.TypeAlias = Field[typing.Any, typing.Any]
"""A field whose entity and value types are not tracked.

For the schema-agnostic machinery -- registry, provider metadata, emission --
which handles fields of every entity type uniformly and has nothing to solve
the parameters against.
"""
