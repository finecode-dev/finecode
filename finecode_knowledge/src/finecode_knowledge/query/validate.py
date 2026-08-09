"""Construction-time validation (§5.8, NFR2/NFR6).

Building a ``Rule`` or ``Query`` validates it against ``FINECODE_SCHEMA``
immediately, so a rule module fails at **import**, not at first execution.

**This is the load-bearing safety mechanism**, not the generics. ADR-0004 D7
means a third party may never run a type checker, so every property the
annotations promise has to be checkable without one. The generics are the
authoring-feedback layer on top: they move the same mistakes from here to the
call site for authors who do run mypy.

Every error names the thing to fix (NFR6). A message that says a rule is
invalid without saying which variable is unbound costs the author the
investigation the checker already did.
"""

from __future__ import annotations

import typing
import warnings

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, Literal, LiteralKind
from finecode_knowledge.query.terms import Prov, Var, term_type_name

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = [
    "UnconstrainedKeyBindingWarning",
    "validate_body",
    "validate_head_parameters",
    "validate_message_default",
]


def _declared_type_name(term: object) -> str | None:
    """The qualified entity type a variable is declared to hold, if it is an entity."""
    if not isinstance(term, Var):
        return None
    qualified = getattr(term.type, "qualified_name", None)
    if callable(qualified) and isinstance(term.type, type):
        return typing.cast(str, qualified())
    return None


def _describe(term: object) -> str:
    if isinstance(term, Var):
        return repr(term)
    return f"constant {term!r}"


def validate_body(
    body: Conjunction,
    schema: SchemaRegistry,
    *,
    context: str,
    projected: typing.Sequence[object] = (),
) -> None:
    """Run every §5.8 check that applies to a body.

    *projected* is the head or projection list, checked for range restriction.
    """
    _check_literals_against_schema(body, schema, context=context)
    _check_negation_safety(body, context=context)
    _check_range_restriction(body, context=context, projected=projected)
    _warn_unconstrained_key_bindings(body, context=context, projected=projected)


def _check_literals_against_schema(
    body: Conjunction, schema: SchemaRegistry, *, context: str
) -> None:
    """Every field literal's entity term matches ``Field.entity``; every edge
    literal's terms match ``Relationship.src``/``dst``.

    A wrong-direction traversal would otherwise return empty, which is
    indistinguishable from a rule that passes -- the silent-wrong failure this
    design rejects everywhere.
    """
    for literal in body:
        if literal.kind is LiteralKind.DERIVED:
            # A predicate validates its own clauses when it is declared; a call
            # site is checked by arity at `__call__`.
            continue

        if literal.kind is LiteralKind.KEY:
            _check_key_literal(literal, schema, context=context)
            continue

        if literal.kind is LiteralKind.KNOWN:
            known_owner = literal.entity_type
            if known_owner is None:
                raise SchemaError(f"{context}: known literal names no entity type.")
            schema.entity_type(known_owner)
            _check_term_type(literal, 0, known_owner, "entity", context)
            continue

        if literal.kind is LiteralKind.FIELD:
            field_owner = literal.entity_type
            if field_owner is None:
                raise SchemaError(
                    f"{context}: field literal {literal.predicate!r} names no entity."
                )
            schema.entity_type(field_owner)
            _check_term_type(literal, 0, field_owner, "entity", context)
            continue

        relationship = schema.relationship(literal.predicate)
        _check_term_type(literal, 0, relationship.src, "src", context)
        _check_term_type(literal, 1, relationship.dst, "dst", context)


def _check_key_literal(
    literal: Literal, schema: SchemaRegistry, *, context: str
) -> None:
    """A KEY literal may name only ``KEY`` fields, and carries no provenance (ADR-0019 D1/D4).

    There is no fact behind it, so there is nothing for ``at=`` to bind. Naming a
    non-KEY field is rejected here as well as at ``EntityType.key()``, because a
    literal can also be built by hand.
    """
    entity_type = literal.entity_type
    if entity_type is None:
        raise SchemaError(f"{context}: key literal names no entity type.")
    declared = [f.id for f in schema.entity_type(entity_type).KEY]
    unknown = [name for name in literal.key_fields if name not in declared]
    if unknown:
        raise SchemaError(
            f"{context}: {entity_type}.key() names {unknown}, which is not part of its "
            f"KEY ({declared}). To read an asserted field use the field literal instead -- "
            "it is a different question (ADR-0019 D4)."
        )
    if literal.at is not None:
        raise SchemaError(
            f"{context}: {entity_type}.key() cannot bind provenance. Addressing an entity "
            "reads no fact, so there is no provenance to bind (ADR-0019 D4)."
        )
    _check_term_type(literal, 0, entity_type, "entity", context)


def _check_term_type(
    literal: Literal, position: int, expected: str, role: str, context: str
) -> None:
    term = literal.terms[position]
    declared = _declared_type_name(term)
    if declared is None or declared == expected:
        # A constant, an untyped variable, or a union head -- none of which this
        # check can refute. Union heads are real (`Var[Preset | Project]`), so
        # rejecting what cannot be proven wrong would reject valid rules.
        return
    raise SchemaError(
        f"{context}: {literal.predicate!r} expects {role} of type {expected!r}, "
        f"but was given {_describe(term)} declared as {declared!r}."
    )


def _check_negation_safety(body: Conjunction, *, context: str) -> None:
    """Every variable in a negated literal must already be bound positively (§5.4).

    Negation is evaluated as a membership test, so an unbound variable inside
    one asks whether *some* binding fails to exist -- which is not what the
    author wrote and cannot be answered by a scan.
    """
    bound: set[int] = set()
    for literal in body:
        terms = (*literal.terms, *((literal.at,) if literal.at is not None else ()))
        if not literal.negated:
            bound.update(id(t) for t in terms if isinstance(t, Var))
            continue
        for term in terms:
            if isinstance(term, Var) and id(term) not in bound:
                raise SchemaError(
                    f"{context}: negated literal {literal.predicate!r} uses "
                    f"{_describe(term)}, which no earlier positive literal binds. "
                    "Bind it before the negation, or factor the existential into a "
                    "`@q.derived` predicate."
                )


def _check_range_restriction(
    body: Conjunction, *, context: str, projected: typing.Sequence[object]
) -> None:
    """Every projected variable must be bound by a positive literal (Datalog range restriction).

    This is also what lets the interpreter never enumerate a type's population:
    if every variable is bound positively, there is nothing to enumerate.
    """
    bound: set[int] = set()
    for literal in body:
        if literal.negated:
            continue
        bound.update(id(t) for t in literal.terms if isinstance(t, Var))
        if isinstance(literal.at, Var):
            bound.add(id(literal.at))

    for term in projected:
        if isinstance(term, Var) and id(term) not in bound:
            kind = "provenance" if isinstance(term, Prov) else "variable"
            raise SchemaError(
                f"{context}: head {kind} {term!r} is not bound by any positive literal "
                f"in the body. Add a literal that binds it"
                + (
                    " (an `at=` on a base literal binds a Prov)."
                    if kind == "provenance"
                    else "."
                )
            )


def validate_head_parameters(
    params: typing.Sequence[str], allowed: typing.Collection[str], *, context: str
) -> None:
    """A rule's head parameter names must be a subset of ``Violation``'s fields (§5.8).

    The names *are* the mapping (ADR-0010), so a typo would otherwise render a
    violation with a silently missing field rather than fail.
    """
    unknown = [name for name in params if name not in allowed]
    if unknown:
        raise SchemaError(
            f"{context}: head parameter(s) {unknown} do not name Violation fields. "
            f"Allowed: {sorted(allowed)}."
        )


def validate_message_default(
    docstring: str | None, params: typing.Sequence[str], *, context: str
) -> None:
    """A rule without ``message=`` needs a docstring containing at least one ``{field}`` (§5.5).

    Guarded rather than silent: without this, forgetting ``message=`` on a rule
    whose docstring explains the *rule* to a developer produces a
    plausible-looking but wrong user-facing string -- and a docstring is under
    no obligation to be a message.
    """
    if not docstring or not docstring.strip():
        raise SchemaError(
            f"{context}: no `message=` and no docstring. Either give the rule a docstring "
            "that reads as the violation message, or pass `@q.rule(message=...)`."
        )
    if not any("{" + name + "}" in docstring for name in params):
        raise SchemaError(
            f"{context}: its docstring contains no {{field}} placeholder naming a head "
            f"parameter ({', '.join(params)}), so it reads as documentation rather than a "
            "message. Pass `@q.rule(message=...)` for the user-facing sentence."
        )


def term_label(term: object) -> str:
    """A readable name for a term, for error messages and rendering."""
    if isinstance(term, Var):
        return f"{type(term).__name__}[{term_type_name(term.type)}]"
    return repr(term)


def _warn_unconstrained_key_bindings(
    body: Conjunction, *, context: str, projected: typing.Sequence[object]
) -> None:
    """Warn where a variable is bound only by KEY literals (ADR-0019 D3).

    A KEY literal binds *identity*, never existence -- it says which entity is
    meant, not that the store knows anything about it. A body that addresses an
    entity and then never constrains it with an edge or field literal is usually
    asking about an entity that may not exist, which is occasionally what the
    author meant and usually is not.

    A warning rather than an error, and deliberately so: under the explicit
    spelling the author wrote ``Type.key(...)`` and can see it. This is for the
    long body where they cannot. Head and projected variables are exempt --
    those are constrained by whoever calls the predicate.
    """
    keyed: set[int] = set()
    constrained: set[int] = set()
    for literal in body:
        if literal.negated:
            continue
        if literal.kind is LiteralKind.KEY:
            term = literal.terms[0]
            if isinstance(term, Var):
                keyed.add(id(term))
            continue
        for term in literal.terms:
            if isinstance(term, Var):
                constrained.add(id(term))

    exempt = {id(term) for term in projected if isinstance(term, Var)}
    dangling = keyed - constrained - exempt
    if dangling:
        warnings.warn(
            f"{context}: {len(dangling)} entity variable(s) bound only by a key literal and "
            "never constrained by an edge or field literal. A key literal binds identity, "
            "not existence -- add a literal that requires the entity to be known, or "
            "confirm that a merely-referenced entity is what you meant (ADR-0019 D3).",
            UnconstrainedKeyBindingWarning,
            stacklevel=3,
        )


class UnconstrainedKeyBindingWarning(UserWarning):
    """See ``_warn_unconstrained_key_bindings``."""
