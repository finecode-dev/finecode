"""Rules, templates, and the shared ``ViolationBuilder`` (ADR-0010, ADR-0018).

**A rule's head parameters are named after ``Violation``'s fields**, so the
mapping needs no strings. Identity and message default from the definition and
both are overridable; the message default is *guarded* rather than silent,
because forgetting ``message=`` on a rule whose docstring explains the rule to a
developer would otherwise produce a plausible-looking but wrong user-facing
string.

**The builder reads nothing** (ADR-0018 D1). It takes the ``Provenance`` each
``Prov`` head parameter is bound to and puts its ``.location`` -- a ``SourceLoc``
-- straight into the ``Violation``. There is no provenance resolver, no store
lookup, and no new literal kind.

That reverses ADR-0010's third bullet, and the reason is worth keeping in view:
rendering a location to ``"dir/file:line"`` needs the defining project's
directory, which is **not an input to whether the rule is violated**. No rule
body predicates on it; moving a project on disk cannot change whether a preset
include is undeclared. Putting that read in the rule's footprint would make a
project move invalidate every memoized violation for that project -- sound, but
a precision loss, and ADR-0010 had recorded it as a correctness gain. Rendering
belongs to whoever displays the violation (``fine_knowledge/locations.py``).
"""

from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import Provenance, SourceLoc
from finecode_knowledge.model.literal import Conjunction, Term
from finecode_knowledge.model.naming import declaring_package
from finecode_knowledge.model.registry import default_registry
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.predicate import _head_signature
from finecode_knowledge.query.query import Query, Result
from finecode_knowledge.query.terms import Prov, Var
from finecode_knowledge.query.validate import (
    validate_body,
    validate_head_parameters,
    validate_message_default,
)

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.query.backend import Backend

__all__ = ["Rule", "Violation", "ViolationBuilder", "rule", "template"]


@dataclasses.dataclass(frozen=True)
class Violation:
    """One finding. Locations are **structural**, not rendered (ADR-0018 D1).

    ``SourceLoc`` is reused rather than paralleled by a second three-field
    record: it already carries exactly ``(project, file, line)``, it is what
    every provider emits, and it is what ``resolve_source_loc`` already
    produces.

    The two location fields answer different questions and both are used --
    in LSP terms they are ``Diagnostic.range`` and ``relatedInformation``:

    - ``asserted_at`` -- where the wrong thing was written.
    - ``expected_in`` -- where the missing thing belongs, or ``None`` when there
      is no such place. The baseline emitted ``""`` here, which was ``Optional``
      in disguise.
    """

    rule: str
    subject: str
    missing: str
    asserted_at: SourceLoc | None
    expected_in: SourceLoc | None
    message: str


VIOLATION_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Violation))
_HEAD_FIELDS: frozenset[str] = frozenset(VIOLATION_FIELDS) - {"rule", "message"}
"""What a rule head may name. ``rule`` and ``message`` come from the definition."""


def _sort_key(violation: Violation) -> tuple:
    """Canonical order, by ``Violation``'s fields in declaration order (ADR-0015 D4).

    A key function rather than ``order=True`` on ``SourceLoc`` (ADR-0018 D6):
    making a model-layer type orderable to serve an output concern is the wrong
    place to pay for it. ``None`` sorts as ``()``.
    """

    def location(loc: SourceLoc | None) -> tuple:
        return () if loc is None else (loc.project or "", loc.file, loc.line)

    return (
        violation.rule,
        violation.subject,
        violation.missing,
        location(violation.asserted_at),
        location(violation.expected_in),
        violation.message,
    )


class ViolationBuilder:
    """Turns a rule's rows into ``Violation``s, in a canonical order.

    One builder for every rule, so a gate's output is stable without any rule
    calling ``.order_by(...)`` (NFR8).
    """

    def __init__(self, rule_id: str, params: tuple[str, ...], message: str) -> None:
        self._rule_id = rule_id
        self._params = params
        self._message = message

    def build(self, rows: typing.Iterable[tuple[object, ...]]) -> list[Violation]:
        violations = [self._one(row) for row in rows]
        violations.sort(key=_sort_key)
        return violations

    def _one(self, row: tuple[object, ...]) -> Violation:
        bound = dict(zip(self._params, row, strict=True))
        return Violation(
            rule=self._rule_id,
            subject=_scalar(bound.get("subject", "")),
            missing=_scalar(bound.get("missing", "")),
            asserted_at=_location(bound.get("asserted_at")),
            expected_in=_location(bound.get("expected_in")),
            message=self._message.format(**{k: _display(v) for k, v in bound.items()}),
        )


def _scalar(value: object) -> str:
    """Render a head binding as the string a ``Violation`` field carries.

    An entity variable renders as its **key**, which is what the baseline did
    (``(subject,) = owning_project_ref.key``) and the only identity an
    ``EntityRef`` has. ``Violation.subject`` is a ``str``, and a repr in a
    user-facing field is the sort of leak ADR-0018 removed from the location
    fields.
    """
    if isinstance(value, EntityRef):
        return value.key[0] if len(value.key) == 1 else "/".join(value.key)
    return str(value)


def _location(value: object) -> SourceLoc | None:
    """A ``Prov`` head parameter is bound to a whole ``Provenance``; take its location."""
    if isinstance(value, Provenance):
        return value.location
    if isinstance(value, SourceLoc):
        return value
    return None


def _display(value: object) -> object:
    """What a ``{field}`` placeholder renders as.

    A ``Provenance`` never reaches a message -- interpolating one would leak a
    repr into a user-facing sentence, and ``SourceLoc.__str__`` is a foot-gun
    besides: it returns the *project-relative* ``file:line``, which is exactly
    the unresolvable bare name the rendering exists to avoid (ADR-0018,
    Consequences).
    """
    if isinstance(value, Provenance | SourceLoc):
        return ""
    if isinstance(value, EntityRef):
        return _scalar(value)
    return value


class Rule:
    """A body plus a ``Violation`` head. Inert until ``violations()`` runs it."""

    def __init__(
        self,
        fn: typing.Callable[..., Conjunction],
        *,
        id: str | None = None,
        message: str | None = None,
        schema: SchemaRegistry | None = None,
    ) -> None:
        params, annotations = _head_signature(fn)
        self.id = id or f"{declaring_package(fn)}.{fn.__name__}"
        self.__doc__ = fn.__doc__
        self._params = params
        self._schema = schema if schema is not None else default_registry()

        context = f"rule {self.id}"
        validate_head_parameters(params, _HEAD_FIELDS, context=context)
        if message is None:
            validate_message_default(fn.__doc__, params, context=context)
            message = typing.cast(str, fn.__doc__).strip()
        self.message = message

        self.location_sensitive = any(annotations[name] is Prov for name in params)
        """Whether this rule's violations can carry a location (ADR-0027 D3).

        Read off the head before the rule runs, which is what makes the
        classification a one-line test rather than a dataflow analysis. A
        sensitive rule cuts off on the bucket verdict alone; an insensitive one
        may additionally cut off on the unit digest, which is sound for it
        precisely because its value cannot contain anything the digest omits
        (D4).

        All three rules in FineCode's own ``validation.py`` take ``Prov`` head
        parameters, so on that workload the strong cut applies to derived
        predicates and to no rule. ADR-0027's Consequences says so; this is where
        it is decided.
        """

        self._head: tuple[Term, ...] = tuple(
            Prov() if annotations[name] is Prov else Var(_inner(annotations[name]))
            for name in params
        )
        body = fn(*self._head)
        if not isinstance(body, Conjunction):
            raise SchemaError(
                f"{context}: a rule body must be a Conjunction -- return `q.all_(...)`, "
                f"got {type(body).__name__}."
            )
        validate_body(body, self._schema, context=context, projected=self._head)
        self._body = body
        self._schema.register_rule(self)

    @property
    def params(self) -> tuple[str, ...]:
        return self._params

    @property
    def query(self) -> Query:
        """The rule's body, projected onto its head."""
        return Query(projection=self._head, body=self._body, schema=self._schema)

    @property
    def version_hash(self) -> str:
        """R8's code-version row for this rule (``query/version.py``).

        Over the body IR **plus** the head parameter names and the message
        template -- unlike a derived predicate, where the names are
        documentation. ``ViolationBuilder`` maps head names onto ``Violation``
        fields by name, so swapping two of them produces different violations
        from identical rows, and the message is user-visible output built from
        the same bindings.
        """
        from finecode_knowledge.query.version import body_version_hash

        return body_version_hash(self._body, head=self._params, extra=self.message)

    async def violations(
        self, backend: Backend, *, mode: Mode = Mode.VERIFIED
    ) -> Result[list[Violation]]:
        """Run the rule and render its rows, carrying the verdict through unchanged."""
        result = await self.query.all(backend, mode=mode)
        builder = ViolationBuilder(self.id, self._params, self.message)
        return Result(value=builder.build(result.value), freshness=result.freshness)

    def __repr__(self) -> str:
        return f"Rule({self.id!r}, params={self._params})"


def _inner(annotation: object) -> object:
    args = typing.get_args(annotation)
    return args[0] if args else object


@typing.overload
def rule(fn: typing.Callable[..., Conjunction], /) -> Rule: ...
@typing.overload
def rule(
    *,
    id: str | None = ...,
    message: str | None = ...,
    schema: SchemaRegistry | None = ...,
) -> typing.Callable[[typing.Callable[..., Conjunction]], Rule]: ...


def rule(
    fn: typing.Callable[..., Conjunction] | None = None,
    /,
    *,
    id: str | None = None,
    message: str | None = None,
    schema: SchemaRegistry | None = None,
) -> typing.Any:
    """Declare a rule. Usable bare (``@q.rule``) or called (``@q.rule(id=...)``)."""
    if fn is not None:
        return Rule(fn)

    def decorate(inner: typing.Callable[..., Conjunction]) -> Rule:
        return Rule(inner, id=id, message=message, schema=schema)

    return decorate


def template(
    *, id: str
) -> typing.Callable[[typing.Callable[..., typing.Any]], typing.Any]:
    """Declare a rule/predicate template. ``id`` is **required** (ADR-0010).

    One template function produces N instantiations, so ``__name__`` cannot
    identify them; each instantiation derives its id from the template's id plus
    its arguments. Falling back to the function name is not permitted.
    """

    def decorate(fn: typing.Callable[..., typing.Any]) -> typing.Any:
        def instantiate(*args: object, **kwargs: object) -> typing.Any:
            produced = fn(*args, **kwargs)
            suffix = "_".join(_arg_id(a) for a in (*args, *kwargs.values()))
            produced.id = (
                f"{declaring_package(fn)}.{id}__{suffix}"
                if suffix
                else f"{declaring_package(fn)}.{id}"
            )
            return produced

        instantiate.template_id = id  # type: ignore[attr-defined]
        instantiate.__name__ = fn.__name__
        instantiate.__doc__ = fn.__doc__
        return instantiate

    return decorate


def _arg_id(value: object) -> str:
    """A stable identity fragment for a template argument."""
    qualified = getattr(value, "qualified_name", None)
    if isinstance(qualified, str):
        return qualified.rpartition(".")[2]
    name = getattr(value, "__name__", None)
    return name if isinstance(name, str) else str(value)
