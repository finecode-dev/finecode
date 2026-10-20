"""Logic variables and bound provenance (§5.1).

``Var`` is compared by **identity**, not by value: two separate ``q.var(Package)``
calls are two different variables even though nothing distinguishes them
structurally, and a body that joined them would be silently wrong. Serialization
(§5.9) and R8's rule-version hash therefore canonicalize by first-appearance
order rather than by variable name -- see ``canonical_names``.
"""

from __future__ import annotations

import itertools
import typing

__all__ = ["Prov", "Term", "Var", "canonical_names", "term_type_name"]

Term: typing.TypeAlias = object
"""A variable, a bound provenance, or a constant -- the same alias ``model/literal.py``
defines, re-stated here so the authoring surface can name it without importing the IR."""

T = typing.TypeVar("T")

_COUNTER = itertools.count()


class Var(typing.Generic[T]):
    """A typed logic variable. *T* is an ``EntityType`` or a value type.

    The parameter is phantom in the same sense as ``Field``'s and
    ``Relationship``'s (ADR-0005): the runtime carries ``type`` as an object,
    and the annotation exists so a rule author gets a call-site type error
    rather than an empty result. Neither is the safety layer --
    construction-time validation (§5.8) is, because ADR-0004 D7 means a third
    party may never run a type checker.
    """

    __slots__ = ("_serial", "type")

    def __init__(self, type_: object = object) -> None:
        self.type = type_
        self._serial = next(_COUNTER)

    def __repr__(self) -> str:
        return f"{type(self).__name__}[{term_type_name(self.type)}]#{self._serial}"


class Prov(Var["object"]):
    """A bound fact-provenance -- a distinct term kind, not a string (ADR-0010).

    Spelled bare in a head signature (``asserted_at: Prov``) because it names
    one thing; the decorator supplies the instance.
    """

    def __init__(self) -> None:
        super().__init__(Prov)


def term_type_name(type_: object) -> str:
    """A readable name for a term's declared type, unions included."""
    name = getattr(type_, "__name__", None)
    return name if isinstance(name, str) else str(type_)


def canonical_names(terms: typing.Iterable[Term]) -> dict[int, str]:
    """Name every variable by first appearance -- ``_0``, ``_1``, ...

    What makes serialization and the rule-version hash stable: two structurally
    identical bodies built from different ``Var`` objects must hash equal, or
    R8's memo key invalidates itself on every import.
    """
    names: dict[int, str] = {}
    for term in terms:
        if isinstance(term, Var) and id(term) not in names:
            names[id(term)] = f"_{len(names)}"
    return names
