"""The IR: a literal, a conjunction of them, and a named predicate (§5.2).

**Data, deliberately.** A literal is a predicate id and two terms; it holds no
reference to the query engine, so ``model/`` gains no dependency on ``query/``
and the import direction stays one-way. A term is typed as ``object`` here for
exactly that reason -- ``Var`` and ``Prov`` live in ``query/terms.py``, and this
layer neither needs nor is allowed to know what they are.

**A body is a value, not a stream** (ADR-0007). ``q.all_(...)`` returns a
``Conjunction``; a predicate is a list of them. Because the whole IR is closed
frozen dataclasses, §5.8's construction-time checks, §5.9's serialization and
R8's rule-version hash are all direct reads of the structure rather than
re-derivations of it.

C1 holds: a literal carries only what ``schema.py`` already declares.
"""

from __future__ import annotations

import dataclasses
import enum
import typing

__all__ = [
    "Body",
    "Clause",
    "Conjunction",
    "Literal",
    "LiteralKind",
    "Predicate",
    "Term",
]

Term: typing.TypeAlias = object
"""A logic variable, a bound provenance, or a constant.

Constants are legal in term position (``PackageFields.name(pkg, "fine_lint")``),
so there is nothing to narrow this to that would not exclude them."""


class LiteralKind(enum.Enum):
    EDGE = "edge"
    FIELD = "field"
    DERIVED = "derived"
    KEY = "key"
    """Addressing: *which* entity is meant (ADR-0019).

    A fourth kind rather than a branch inside ``FIELD``, because the two differ
    on four counts at once -- existence, provenance, footprint membership and
    contestability -- and a difference that large disambiguated by a mental
    lookup in ``schema.py`` is the conflation ADR-0019 exists to remove:

    | | ``Type.key(e, f=v)`` | ``Fields.f(e, v)`` |
    | --- | --- | --- |
    | Existence | none implied | the entity is known |
    | Provenance | none -- nobody asserted it | a ``Provenance`` per fact |
    | Footprint | reads no slot | reads a slot |
    | Contestable | no -- two values are two entities | yes (C9) |

    "Every preset some ``preset.toml`` names" and "every preset we actually
    scanned" are different sets, and their difference is the subject matter of
    all three project rules."""
    KNOWN = "known"
    """Existence: is this entity one the store has *any* fact about?

    The complement of ``KEY``, and the row the table above shows was missing a
    spelling. ``KEY`` addresses without implying existence and ``FIELD`` implies
    existence but only of a *named* field, so "does this entity exist at all"
    had no literal -- and ADR-0013 D3 had already named ``FactSource.contains``
    as "the one existence question the scans cannot answer".

    That gap is not academic: it is what forced ``projection.py`` to hold a
    ``FactSource`` and call ``contains`` outside the read channel, which is the
    R21 violation Phase 1b closed. A read the query language cannot express is a
    read somebody will express another way.

    Records an ``("entity", ref)`` footprint key, which is invalidated by any
    fact about that entity -- the correct dependency for "does it exist", since
    a first fact about it is exactly what would change the answer."""


@dataclasses.dataclass(frozen=True)
class Literal:
    """One binary predicate applied to two terms, optionally negated."""

    kind: LiteralKind
    predicate: str
    """Qualified: an edge kind, a field's qualified name, or a predicate's dotted path."""
    terms: tuple[Term, ...]
    entity_type: str | None = None
    """``FIELD`` and ``KEY`` -- the qualified entity type addressed.

    A field is identified by the *pair*: ``name`` exists on ``Package``,
    ``Handler`` and ``Environment``, so the field name alone does not say which
    slot this literal reads (ADR-0013 D3)."""
    key_fields: tuple[str, ...] = ()
    """``KEY`` only -- which ``KEY`` fields the value terms name, in ``KEY``
    declaration order. ``terms`` is ``(entity, *values)``.

    A key *is* a tuple, which is why this is not split into one binary literal
    per component: on a multi-field key (``Handler``, ``Environment``) the
    construct direction would then need cross-literal analysis to know when
    every component had bound, making it inexpressible rather than deferred
    (ADR-0019, alternative 4)."""
    at: Term | None = None
    """Binds the fact's provenance as a term (FR5).

    Provenance is an ordinary bindable term, checkable like any other, rather
    than a stringly-typed side channel. Base literals carry it here; a derived
    predicate that exposes provenance does so as a named head parameter
    instead, because its provenance is whichever of its body's facts it chose
    to expose."""
    negated: bool = False

    def negate(self) -> Literal:
        return dataclasses.replace(self, negated=True)

    @property
    def slot(self) -> tuple[str, ...]:
        """The predicate's identity, independent of its terms."""
        if self.kind is LiteralKind.FIELD:
            return (self.kind.value, self.entity_type or "", self.predicate)
        if self.kind is LiteralKind.KEY:
            return (self.kind.value, self.entity_type or "", *self.key_fields)
        return (self.kind.value, self.predicate)


@dataclasses.dataclass(frozen=True)
class Conjunction:
    """A body: literals that must all hold, in written order."""

    literals: tuple[Literal, ...]

    def __iter__(self) -> typing.Iterator[Literal]:
        return iter(self.literals)

    def __len__(self) -> int:
        return len(self.literals)


Body: typing.TypeAlias = Conjunction


@dataclasses.dataclass(frozen=True)
class Clause:
    """One clause's head terms paired with its body.

    Each clause is written as its own ``def`` and so owns its own head
    variables; the engine unifies a call's terms against ``head`` and renames
    everything else in ``body`` apart. A pairing rather than a fourth IR shape.
    """

    head: tuple[Term, ...]
    body: Conjunction


@dataclasses.dataclass(frozen=True)
class Predicate:
    """A named, reusable body of literals -- one or more clauses.

    **Clauses are disjunction, not a new operator** (§5.3): the predicate's
    value is the union of its clauses' results. Rule 3's
    ``for kind in ("includes_preset", "uses_preset")`` is the concrete union
    that invalidated the conjunction-only assumption.
    """

    id: str
    """The registered name: the qualified dotted path §5.9 serializes by."""
    params: tuple[str, ...]
    """Head parameter names, taken from the ``def`` signature. There is no
    separate declaration of shape -- a second declaration is exactly what
    ADR-0007 removed and what C1 forbids at the schema level."""
    clauses: tuple[Clause, ...]

    @property
    def arity(self) -> int:
        return len(self.params)
