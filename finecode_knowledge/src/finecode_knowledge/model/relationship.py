from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import EdgeFact, Provenance
from finecode_knowledge.model.literal import Literal, LiteralKind, Term

__all__ = ["AnyRelationship", "Relationship"]

S = typing.TypeVar("S")
D = typing.TypeVar("D")


@dataclasses.dataclass(frozen=True)
class Relationship(typing.Generic[S, D]):
    """A relationship from entity type *S* to entity type *D*.

    Both parameters are phantom -- the runtime carries ``src``/``dst`` as
    strings -- and exist to move wrong-direction traversal from a runtime
    ``SchemaError`` (or worse, an empty result that reads as a passing rule) to
    a type error at the call site (ADR-0005).

    Declare relationships in **annotation** position, never by subscripting the
    constructor::

        class Rel:
            provides_package: Relationship[Project, Package] = Relationship(
                "provides_package", "Project", "Package", band=Band.DECLARED, upper=1
            )

    ``Rel`` is declared before the entity classes, which reference ``Rel.*`` in
    their ``RELATIONS`` -- naming the entity classes in value position would
    invert that cycle. Under ``from __future__ import annotations`` the
    subscript is never evaluated at runtime, so the forward reference resolves
    for a type checker while declaration order stays as it is.

    As with ``Field``, that is how a *declared* schema spells it: constructing a
    relationship ad hoc -- ``Relationship("contains", "Widget", "Gizmo", ...)``,
    no ``Rel`` class and no entity classes -- stays first-class, and ``src``/
    ``dst`` remain what the runtime reads.

    See ``model/fields.py`` for the same mechanism on ``Field``,
    ``tests/test_schema_typing.py`` for the reflection test that keeps the
    annotation and the strings from drifting, and ADR-0005 for why the
    duplication is kept.

    ``package`` is bound at registration and ``src``/``dst`` are rewritten to
    their qualified forms then, exactly as for ``Field`` -- see that class's
    docstring and ADR-0017 D3/D4 for why the qualifier is derived rather than
    typed.
    """

    kind: str
    src: str
    dst: str
    band: Band
    lower: int = 0
    upper: int = 1
    opposite: str | None = None
    containment: bool = False
    package: str | None = dataclasses.field(default=None, compare=False)
    """The declaring package, bound at registration. Excluded from identity for
    the same reason as ``Field.package``."""

    @property
    def qualified_name(self) -> str:
        """``<declaring package>.<kind>`` -- how the store and every edge fact spell this kind."""
        if self.package is None:
            raise SchemaError(
                f"Relationship {self.kind!r} has no qualified name: it was never registered. "
                "Register its declaring class with `register_namespace`, or the "
                "relationship itself with `register_relationship(r, package=...)`."
            )
        return f"{self.package}.{self.kind}"

    def _bind(self, package: str, src: str, dst: str) -> None:
        """Attach the derived qualifier (ADR-0017 D3). Called only by ``SchemaRegistry``."""
        if self.package is not None and self.package != package:
            raise SchemaError(
                f"Relationship {self.kind!r} is already declared by {self.package!r}; "
                f"{package!r} cannot re-declare the same object. Declare a separate "
                "Relationship instance in the second package."
            )
        object.__setattr__(self, "package", package)
        object.__setattr__(self, "src", src)
        object.__setattr__(self, "dst", dst)

    def __call__(self, src: Term, dst: Term, *, at: Term | None = None) -> Literal:
        """An edge literal: *src* relates to *dst* through this relationship (FR4).

        ``Relationship`` is generic (ADR-0005), so calling it the wrong way
        round is a type error at the call site rather than an empty result that
        reads as a passing rule. Construction-time validation (§5.8) catches it
        again for the case where no type checker runs.
        """
        return Literal(
            kind=LiteralKind.EDGE,
            predicate=self.qualified_name,
            terms=(src, dst),
            at=at,
        )

    def edge(self, src: EntityRef, dst: EntityRef, prov: Provenance) -> EdgeFact:
        if src.type != self.src:
            raise SchemaError(
                f"{self.kind}: expected src type {self.src!r}, got {src.type!r}"
            )
        if dst.type != self.dst:
            raise SchemaError(
                f"{self.kind}: expected dst type {self.dst!r}, got {dst.type!r}"
            )
        # The *local* kind, deliberately: an emission is qualified by `FactStore.ingest`
        # against the emitting provider's SUPPLIES declarations (ADR-0017 D6), which is
        # the one place that knows both the name and who is entitled to emit it. Keeping
        # it out of `edge()` is what lets an unregistered, ad-hoc Relationship still
        # build facts (ADR-0005), and leaves exactly one authority on qualification.
        return EdgeFact(kind=self.kind, src=src, dst=dst, prov=prov)


AnyRelationship: typing.TypeAlias = Relationship[typing.Any, typing.Any]
"""A relationship whose endpoint types are not tracked.

For the schema-agnostic machinery -- registry, provider metadata, ``SUPPLIES``
-- which handles relationships of every entity type uniformly and has nothing
to solve the parameters against.
"""
