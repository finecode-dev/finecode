"""Derived predicates: named, reusable bodies with inferred shape (ADR-0012 D-A).

Calling ``owns_preset(a, b)`` **does not run the body** -- it returns a
``Literal`` whose predicate is this one. The engine expands it, renaming body
variables apart. Three things follow, and the first is the point of the whole
fold:

- **Identical at the call site to a base relation** (FR2). The
  ``store.resolve()`` / ``store.targets_of()`` split disappears -- a rule body
  cannot tell whether ``uses_preset`` is stored or derived, and does not have to.
- **Recursion is expressible** (FR10): because the engine expands the body,
  self-reference is a fixpoint rather than infinite inlining.
- **Head types come from the ``def`` signature.** Nothing is re-declared.
  ``@q.derived`` takes no ``kind``/``src``/``dst``: an explicit triple would
  reintroduce exactly what ADR-0007 removed, and could drift from the signature
  it duplicates with nothing to catch the drift.
"""

from __future__ import annotations

import enum
import inspect
import typing

from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import (
    Clause,
    Conjunction,
    Literal,
    LiteralKind,
    Predicate,
)
from finecode_knowledge.model.naming import declaring_package
from finecode_knowledge.model.registry import default_registry
from finecode_knowledge.query.terms import Prov, Var
from finecode_knowledge.query.validate import validate_body

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry

__all__ = ["DerivedPredicate", "PredicateShape", "derived"]


class PredicateShape(enum.Enum):
    """How a predicate's head reads as an ER element (ADR-0012 D-A).

    Computed from the head alone. ``UNREPRESENTABLE`` is a real answer, not a
    failure: such predicates are named in a comment line rather than silently
    dropped, because addressability is one of the reasons the fold is worth
    doing and a predicate the diagram cannot name is not addressable.
    """

    EDGE = "edge"
    ATTRIBUTE = "attribute"
    UNREPRESENTABLE = "unrepresentable"


def _entity_type_name(annotation: object) -> str | None:
    """The qualified entity type a ``Var[E]`` annotation names, if it names exactly one."""
    args = typing.get_args(annotation)
    if not args:
        return None
    inner = args[0]
    qualified = getattr(inner, "qualified_name", None)
    if callable(qualified) and isinstance(inner, type):
        return typing.cast(str, qualified())
    return None


def _is_value_var(annotation: object) -> bool:
    args = typing.get_args(annotation)
    return bool(args) and _entity_type_name(annotation) is None


class DerivedPredicate:
    """A predicate a rule body calls exactly as it calls ``Rel.serves``."""

    def __init__(
        self,
        fn: typing.Callable[..., Conjunction],
        *,
        id: str | None = None,
        schema: SchemaRegistry | None = None,
    ) -> None:
        self._params, self._annotations = _head_signature(fn)
        self.id = id or f"{declaring_package(fn)}.{fn.__name__}"
        self.__doc__ = fn.__doc__
        self._schema = schema if schema is not None else default_registry()
        self._clauses: list[Clause] = []
        self._add_clause(fn, self._params, self._annotations)

    # ---- authoring ----------------------------------------------------

    def clause(self, fn: typing.Callable[..., Conjunction]) -> DerivedPredicate:
        """Add a clause -- disjunction, spelled as a second definition (§5.3).

        The predicate's value is the union of its clauses' results. Clauses need
        not agree on their head *types*: ``references_preset``'s two clauses
        force ``Var[Preset | Project]``, which is honest rather than elegant and
        is the one place the examples strain the typing.
        """
        params, annotations = _head_signature(fn)
        if len(params) != len(self._params):
            raise SchemaError(
                f"{self.id}: clause has arity {len(params)}, but the predicate has "
                f"arity {len(self._params)} ({', '.join(self._params)})."
            )
        self._add_clause(fn, params, annotations)
        return self

    def _add_clause(
        self,
        fn: typing.Callable[..., Conjunction],
        params: tuple[str, ...],
        annotations: dict[str, object],
    ) -> None:
        clause = _build_clause(fn, params, annotations)
        # Validated at declaration, so a rule module fails at import rather than at
        # first execution (§5.8). Range restriction is checked against this clause's
        # own head: every head term must be bound by a positive literal in its body.
        validate_body(
            clause.body,
            self._schema,
            context=f"predicate {self.id}",
            projected=clause.head,
        )
        self._clauses.append(clause)

    def __call__(self, *args: object, **kwargs: object) -> Literal:
        bound = _bind_head(self.id, self._params, args, kwargs)
        return Literal(kind=LiteralKind.DERIVED, predicate=self.id, terms=bound)

    # ---- inspection ---------------------------------------------------

    @property
    def params(self) -> tuple[str, ...]:
        return self._params

    @property
    def predicate(self) -> Predicate:
        """The IR this predicate denotes."""
        return Predicate(id=self.id, params=self._params, clauses=tuple(self._clauses))

    @property
    def version_hash(self) -> str:
        """R8's code-version row for this definition (``query/version.py``).

        Over the clause IR, with head parameter *names* excluded: the engine
        unifies positionally, so renaming a head parameter changes nothing this
        predicate computes. Recomputed per call rather than cached because
        ``clause()`` may add a clause after construction, and a hash that went
        stale the moment a second clause landed would be worse than no hash.
        """
        from finecode_knowledge.query.version import clauses_version_hash

        return clauses_version_hash(tuple(self._clauses))

    @property
    def location_sensitive(self) -> bool:
        """Whether this predicate's value can contain a location (ADR-0027 D3).

        A ``Prov`` in the head is the whole test, and it is *local*: the head is a
        complete description of what a memoized value contains, so a predicate
        that consumes a sensitive one without projecting its ``Prov`` into its own
        head cannot carry a location and stays insensitive. Sensitivity travels
        exactly where a location travels.

        An insensitive node may cut off on the unit digest; a sensitive one may
        not, because the digest omits precisely what such a node exposes (D4).
        """
        return any(self._annotations[name] is Prov for name in self._params)

    @property
    def shape(self) -> PredicateShape:
        """This predicate's ER classification, inferred from the head signature."""
        if len(self._params) != 2:
            return PredicateShape.UNREPRESENTABLE
        first, second = (self._annotations[p] for p in self._params)
        if first is Prov or second is Prov:
            return PredicateShape.UNREPRESENTABLE
        src = _entity_type_name(first)
        if src is None:
            return PredicateShape.UNREPRESENTABLE
        if _entity_type_name(second) is not None:
            return PredicateShape.EDGE
        if _is_value_var(second):
            return PredicateShape.ATTRIBUTE
        return PredicateShape.UNREPRESENTABLE

    @property
    def endpoints(self) -> tuple[str | None, str | None]:
        """``(src, dst)`` qualified entity type names, where the head names them."""
        if len(self._params) != 2:
            return (None, None)
        first, second = (self._annotations[p] for p in self._params)
        return (_entity_type_name(first), _entity_type_name(second))

    def __repr__(self) -> str:
        return f"DerivedPredicate({self.id!r}, params={self._params})"


def _head_signature(
    fn: typing.Callable[..., Conjunction],
) -> tuple[tuple[str, ...], dict[str, object]]:
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=False)
    params = tuple(signature.parameters)
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise SchemaError(
                f"{fn.__qualname__}: *args/**kwargs cannot be a predicate head -- "
                "the head signature is the only declaration of the predicate's shape."
            )
        if name not in hints:
            raise SchemaError(
                f"{fn.__qualname__}: head parameter {name!r} has no annotation. "
                "Annotate it `Var[<type>]` or `Prov`; the head signature is the only "
                "declaration of the predicate's shape (ADR-0012 D-A)."
            )
    return params, {name: hints[name] for name in params}


def _build_clause(
    fn: typing.Callable[..., Conjunction],
    params: tuple[str, ...],
    annotations: dict[str, object],
) -> Clause:
    """Call *fn* once, with fresh head variables, and keep what it returns.

    The body is evaluated exactly once, at decoration -- it is a *value*
    (ADR-0007), so there is nothing to re-run per call and nothing that could
    return a different body the second time.
    """
    head = tuple(_fresh_term(annotations[name]) for name in params)
    body = fn(*head)
    if not isinstance(body, Conjunction):
        raise SchemaError(
            f"{fn.__qualname__}: a predicate body must be a Conjunction -- "
            f"return `q.all_(...)`, got {type(body).__name__}."
        )
    return Clause(head=head, body=body)


def _fresh_term(annotation: object) -> Var:
    if annotation is Prov:
        return Prov()
    args = typing.get_args(annotation)
    return Var(args[0] if args else object)


def _bind_head(
    predicate_id: str,
    params: tuple[str, ...],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> tuple[object, ...]:
    if len(args) > len(params):
        raise SchemaError(
            f"{predicate_id} takes {len(params)} term(s) ({', '.join(params)}), got {len(args)}."
        )
    bound: dict[str, object] = dict(zip(params, args, strict=False))
    for name, value in kwargs.items():
        if name not in params:
            raise SchemaError(
                f"{predicate_id} has no head parameter {name!r}; it takes "
                f"({', '.join(params)})."
            )
        if name in bound:
            raise SchemaError(f"{predicate_id} got two values for {name!r}.")
        bound[name] = value
    missing = [name for name in params if name not in bound]
    if missing:
        raise SchemaError(f"{predicate_id} is missing term(s) {missing}.")
    return tuple(bound[name] for name in params)


def derived(fn: typing.Callable[..., Conjunction]) -> DerivedPredicate:
    """Declare a reusable body. The head signature is the whole declaration."""
    return DerivedPredicate(fn)
