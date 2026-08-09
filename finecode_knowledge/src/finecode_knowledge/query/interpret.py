"""The direct interpreter -- the only backend that executes (ADR-0015 D1).

Execute literals in written order against an indexed ``FactSource``, expand
derived predicates on demand, union clauses, evaluate negation as a membership
test. **No join reordering, no planner.** §3.6 measured rule inputs at 32/33
edges and found query performance is not a constraint at either 2.3k or 148k
facts; the O(n^2) behaviour it *did* identify is the linear scan, which the
store's index removes.

``run`` is an ``async def`` that never awaits (ADR-0016 D3). The walk and the
``FactSource`` stay synchronous, so a whole execution sees a consistent snapshot
for free -- no MVCC, no locks -- and thread-offload stays a one-line change
*inside* ``run``::

    return await asyncio.to_thread(self._execute, query, mode, limit)

That is safe precisely because the walk is synchronous, performs no I/O, and
reads a snapshot nothing else mutates. The trigger for taking it is a single
execution exceeding ~50 ms at §3.6's projected scale.

**Cancellation attaches to verification and extraction, not here.** A
synchronous walk has no await point at which to check a signal, so it is
deliberately uncancellable and deliberately short; the long, awaiting work is
sequenced ahead of it by verify-then-execute (ADR-0013 D6.3).
"""

from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, Literal, LiteralKind
from finecode_knowledge.model.verify import Verdict
from finecode_knowledge.query import attribution
from finecode_knowledge.query import footprint as fp
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.freshness import (
    Conflict,
    Freshness,
    Reservation,
    ReservationKind,
    contested_slot,
    unit_reservation,
    untracked_fact_file,
)
from finecode_knowledge.query.query import Result
from finecode_knowledge.query.terms import Prov, Var

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from finecode_knowledge.model.fact_source import FactSource, Record
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.model.verify import VerifyReport
    from finecode_knowledge.query.query import Query

__all__ = ["MAX_EXPANSION_DEPTH", "InterpreterBackend"]

MAX_EXPANSION_DEPTH = 32
"""How deep derived-predicate expansion may nest before it is called recursion.

FR10 makes recursion *expressible* -- the engine expands bodies, so
self-reference is a fixpoint rather than infinite inlining -- but evaluating a
fixpoint is not built (§5.7). Rather than hang, a body that recurses past this
depth raises and says so."""

_UNBOUND = object()

Bindings: typing.TypeAlias = "dict[int, object]"


_RESERVATION_OF: dict[Verdict, ReservationKind] = {
    Verdict.STALE: ReservationKind.STALE,
    Verdict.UNTRACKED: ReservationKind.UNTRACKED,
    # ADR-0022 D3: `MISSING` is a distinct *bucket verdict* -- a deleted source
    # is what a future retraction acts on (§4.7) and a changed one is not -- but
    # it is not a distinct reservation kind, because no caller acts differently
    # on it. The distinction survives in `detail`.
    Verdict.MISSING: ReservationKind.STALE,
}


@dataclasses.dataclass
class _Execution:
    """Everything one execution accumulates. Fresh per ``run`` call."""

    source: FactSource
    schema: SchemaRegistry
    collector: fp.FootprintCollector
    conflicts: dict[tuple[str, str, tuple[str, ...]], Conflict] = dataclasses.field(
        default_factory=dict
    )
    consulted: set[tuple[str, str, object]] = dataclasses.field(default_factory=set)
    """Field slots already checked for contest -- once per distinct slot, not per row."""


class InterpreterBackend:
    """Runs a ``Query`` against a ``FactSource`` in this process."""

    def __init__(
        self,
        source: FactSource,
        *,
        schema: SchemaRegistry | None = None,
        verdicts: VerifyReport | None = None,
    ) -> None:
        self._source = source
        self._schema = schema
        self._verdicts = verdicts
        """The cold-start walk's per-bucket result, or ``None`` if none was run.

        This replaces the ``extraction_tracked: bool`` placeholder ADR-0014 D7
        left for the memo DAG. R11 fills it earlier than that record expected:
        persisted fingerprints make buckets tracked without a DAG, so the flag's
        job -- "is the link from the served facts back to their sources
        tracked?" -- is now answered per bucket rather than for the whole store.

        ``None`` means no verification was run, which is not the same as
        "everything is fine": it falls back to D7's standing reservation, since
        a store nobody checked is exactly the store D7 was written about."""

    async def run(
        self, query: Query, *, mode: Mode = Mode.VERIFIED, limit: int | None = None
    ) -> Result[list[tuple[object, ...]]]:
        return self._execute(query, mode=mode, limit=limit)

    async def records(self, refs: Sequence[EntityRef]) -> tuple[Record, ...]:
        """Everything known about each ref, in one pass (``query/records.py``, R21).

        **Recorded in a footprint like any other read**, which is the whole reason
        this method exists rather than callers holding the ``FactSource``. An
        ``("entity", ref)`` key is invalidated by any fact about that entity and
        attributes to every provider supplying its type, so a projection built
        from these records is invalidated on the same terms a rule is. A read
        that skipped the collector would contribute no key and its consumer would
        never be told the world moved -- ADR-0013 D4.3, one layer up.

        The collector is fresh per call and lands in ``last_footprint``, exactly
        as an execution's does: both are "what the most recent read consulted",
        and the memo layer wants them on the same terms.
        """
        collector = fp.FootprintCollector()
        found: list[Record] = []
        for ref in refs:
            collector.record(fp.entity_key(ref))
            found.append(self._source.record(ref))
        self.last_footprint = collector
        return tuple(found)

    def _execute(
        self, query: Query, *, mode: Mode, limit: int | None
    ) -> Result[list[tuple[object, ...]]]:
        execution = _Execution(
            source=self._source,
            schema=self._schema or query.schema,
            collector=fp.FootprintCollector(),
        )

        rows: dict[tuple[object, ...], None] = {}
        for bindings in _solve(query.body.literals, {}, execution, depth=0):
            rows.setdefault(
                tuple(_resolve(term, bindings) for term in query.projection), None
            )
            if limit is not None and len(rows) >= limit:
                break

        self.last_footprint = execution.collector
        """The footprint of the most recent execution.

        Handed to the memo layer once at the terminal (ADR-0013 D5) -- "collected
        per primitive, reported once per query", verbatim. It does not ride the
        `Result`: a footprint is one key per access, the wrong size for a value
        whose job is to be read (ADR-0014 D3)."""

        return Result(
            value=list(rows),
            freshness=self._verdict(mode, execution),
        )

    def _verdict(self, mode: Mode, execution: _Execution) -> Freshness:
        revision = self._source.revision
        reservations: list[Reservation] = []
        reservations.extend(self._input_reservations(execution))
        reservations.extend(contested_slot(c) for c in execution.conflicts.values())
        # No CACHED reservation is reachable **from here**, and since Phase 5 that
        # is structural rather than pending. `CACHED` means "this was not
        # recomputed", so only the layer that declined to recompute can assert it
        # -- `memo/walk.py`, on the branch where it serves a value it did not
        # verify. An execution, by definition, just did the work, so whichever
        # mode asked for it the answer it produces is a verified one. The memo
        # walk pins recomputation to `Mode.VERIFIED` for the same reason.
        #
        # `STALE` *is* reachable here -- ADR-0022 D2 widens the verified-mode
        # contract to admit it, because "the sources moved" is something verified
        # mode can check and must say.
        del mode
        return Freshness(revision=revision, reservations=tuple(reservations))

    def _input_reservations(self, execution: _Execution) -> list[Reservation]:
        """One reservation per unconfirmed bucket this query could have read.

        *Could*, not *did* -- attribution is static, over the schema, because a
        scan that came back empty depends on the buckets that might have filled
        it (`query/attribution.py`). Narrowing to the rows returned is silent in
        exactly the direction that stops a real violation being reported.
        """
        if self._verdicts is None:
            return [untracked_fact_file(self._source.revision)]

        unconfirmed = self._verdicts.unconfirmed()
        if not unconfirmed:
            return []

        schema = execution.schema
        covered = attribution.providers_for_footprint(execution.collector.keys, schema)
        return [
            unit_reservation(
                kind=_RESERVATION_OF[verdict.kind],
                unit_id=unit_id,
                provider_id=provider_id,
                detail=verdict.detail,
            )
            for (provider_id, unit_id), verdict in unconfirmed.items()
            if provider_id in covered
        ]


# ---- the walk ---------------------------------------------------------


def _solve(
    literals: tuple[Literal, ...], bindings: Bindings, ctx: _Execution, *, depth: int
) -> Iterator[Bindings]:
    if not literals:
        yield bindings
        return
    head, rest = literals[0], literals[1:]
    for extended in _match(head, bindings, ctx, depth=depth):
        yield from _solve(rest, extended, ctx, depth=depth)


def _match(
    literal: Literal, bindings: Bindings, ctx: _Execution, *, depth: int
) -> Iterator[Bindings]:
    if literal.negated:
        positive = dataclasses.replace(literal, negated=False)
        # A membership test: any solution at all means the negation fails. It binds
        # nothing -- which is why §5.8 requires every negated variable to be bound
        # positively earlier.
        for _ in _match(positive, bindings, ctx, depth=depth):
            return
        yield bindings
        return

    if literal.kind is LiteralKind.EDGE:
        yield from _match_edge(literal, bindings, ctx)
    elif literal.kind is LiteralKind.FIELD:
        yield from _match_field(literal, bindings, ctx)
    elif literal.kind is LiteralKind.KEY:
        yield from _match_key(literal, bindings, ctx)
    elif literal.kind is LiteralKind.KNOWN:
        yield from _match_known(literal, bindings, ctx)
    else:
        yield from _match_derived(literal, bindings, ctx, depth=depth)


def _match_known(
    literal: Literal, bindings: Bindings, ctx: _Execution
) -> Iterator[Bindings]:
    """Does the store hold any fact about this entity? (``LiteralKind.KNOWN``)

    A filter, never a generator: the entity has to be bound already, because
    binding one from here would mean enumerating every reference the store has
    seen -- ADR-0019 D6's refusal, for the same reason.

    Records an ``("entity", ref)`` key **whether or not the entity exists**. The
    empty case is the one that matters: "this action is unknown" is an answer
    resting on the absence of facts, and a first fact about it is exactly what
    would change that answer. Recording only on a hit is ADR-0013 D4.3's silent
    false negative in miniature.
    """
    entity_type = literal.entity_type
    if entity_type is None:
        raise SchemaError("known literal names no entity type")
    ref = _resolve(literal.terms[0], bindings)
    if not isinstance(ref, EntityRef):
        raise SchemaError(
            f"{entity_type}.known() needs its entity bound by an earlier literal. It "
            "filters references down to the ones the store knows about; it cannot "
            "produce them, because that would mean enumerating every reference ever "
            "seen (ADR-0019 D6)."
        )
    ctx.collector.record(fp.entity_key(ref))
    if ctx.source.contains(entity_type, ref):
        yield bindings


def _match_edge(
    literal: Literal, bindings: Bindings, ctx: _Execution
) -> Iterator[Bindings]:
    src = _resolve(literal.terms[0], bindings)
    dst = _resolve(literal.terms[1], bindings)
    src_filter = _ref_filter(src)
    dst_filter = _ref_filter(dst)
    if _impossible(src, src_filter) or _impossible(dst, dst_filter):
        return

    ctx.collector.record(fp.edge_key(literal.predicate, src_filter, dst_filter))
    for fact in ctx.source.edge_facts(
        literal.predicate, src=src_filter, dst=dst_filter
    ):
        extended = _bind(bindings, literal.terms[0], fact.src)
        if extended is None:
            continue
        extended = _bind(extended, literal.terms[1], fact.dst)
        if extended is None:
            continue
        if literal.at is not None:
            extended = _bind(extended, literal.at, fact.prov)
            if extended is None:
                continue
        yield extended


def _match_field(
    literal: Literal, bindings: Bindings, ctx: _Execution
) -> Iterator[Bindings]:
    entity_type = literal.entity_type
    if entity_type is None:
        raise SchemaError(f"field literal {literal.predicate!r} names no entity type")
    entity = _resolve(literal.terms[0], bindings)
    value = _resolve(literal.terms[1], bindings)
    entity_filter = _ref_filter(entity)
    if _impossible(entity, entity_filter):
        return
    value_filter = None if value is _UNBOUND else value

    ctx.collector.record(
        fp.field_key(entity_type, literal.predicate, entity_filter, value_filter)
    )
    _consult_conflicts(literal, entity_type, entity_filter, ctx)

    for fact in ctx.source.field_facts(
        entity_type, literal.predicate, entity=entity_filter, value=value_filter
    ):
        extended = _bind(bindings, literal.terms[0], fact.entity)
        if extended is None:
            continue
        extended = _bind(extended, literal.terms[1], fact.value)
        if extended is None:
            continue
        if literal.at is not None:
            extended = _bind(extended, literal.at, fact.prov)
            if extended is None:
                continue
        yield extended


def _consult_conflicts(
    literal: Literal, entity_type: str, entity: EntityRef | None, ctx: _Execution
) -> None:
    """Check the slot for contest once, and record the **value-unbound** key.

    Once per distinct slot, not per row (ADR-0014 D4). The value-unbound key
    ``("field", t, f, e, None)`` is what makes a conflicting fact arriving later
    match under the wildcard rule -- a value-*bound* key would not, which is
    exactly the hole that made interpreter-side detection unsound.
    """
    slot = (entity_type, literal.predicate, entity)
    if slot in ctx.consulted:
        return
    ctx.consulted.add(slot)
    ctx.collector.record(fp.field_key(entity_type, literal.predicate, entity, None))
    for conflict in ctx.source.conflicts(entity_type, literal.predicate, entity=entity):
        ctx.conflicts.setdefault(
            (conflict.entity.type, conflict.field, conflict.entity.key), conflict
        )


def _match_key(
    literal: Literal, bindings: Bindings, ctx: _Execution
) -> Iterator[Bindings]:
    """Resolve an addressing literal against the reference itself (ADR-0019 D2).

    Three directions, and **no ``FactSource`` call in any of them** -- the
    literal is a pure function of its bindings:

    - **entity bound -> project.** Each named component binds to its slot in
      ``entity.key``. Any subset may be named; a component whose term is already
      bound is *tested* instead, and the row survives only if they agree.
    - **entity unbound, every KEY field named and bound -> construct.** Exactly
      the baseline's ``Package.ref(name=...)``, which is what §6.1 claimed and
      what ADR-0019 makes true.
    - **anything else -> ``SchemaError``** (D6): binding an entity from a
      partial key would mean enumerating every reference the store has seen,
      including endpoints carrying no facts, and that is a new seam member with
      no caller yet.

    Records no footprint key and consults no conflicts (D5). Both omissions are
    *precise* rather than under-approximations: no stored fact can change this
    literal's answer, and a KEY field cannot be contested because two different
    key values are two different entities.
    """
    entity_type = literal.entity_type
    if entity_type is None:
        raise SchemaError("key literal names no entity type")
    entity_class = ctx.schema.entity_type(entity_type)
    key_ids = [f.id for f in entity_class.KEY]
    entity = _resolve(literal.terms[0], bindings)

    if entity is not _UNBOUND:
        if not isinstance(entity, EntityRef) or entity.type != entity_type:
            return
        extended: Bindings | None = bindings
        for name, term in zip(literal.key_fields, literal.terms[1:], strict=True):
            extended = _bind(extended, term, entity.key[key_ids.index(name)])
            if extended is None:
                return
        yield extended
        return

    values = [_resolve(term, bindings) for term in literal.terms[1:]]
    unnamed = [name for name in key_ids if name not in literal.key_fields]
    unbound = [
        name
        for name, value in zip(literal.key_fields, values, strict=True)
        if value is _UNBOUND
    ]
    if unnamed or unbound:
        raise SchemaError(
            f"{entity_type}.key(): cannot address an entity from a partial key -- "
            f"unnamed {unnamed}, unbound {unbound}. Either bind the entity with an edge "
            f"or field literal first, or name and bind every KEY field {key_ids}."
        )
    ref = entity_class.ref(**dict(zip(literal.key_fields, values, strict=True)))
    bound = _bind(bindings, literal.terms[0], ref)
    if bound is not None:
        yield bound


def _match_derived(
    literal: Literal, bindings: Bindings, ctx: _Execution, *, depth: int
) -> Iterator[Bindings]:
    """Expand the predicate, renaming body variables apart, and union its clauses.

    Expansion is what makes a derived call identical to a base one at the call
    site (FR2) and what makes recursion a fixpoint rather than infinite inlining
    (FR10) -- though evaluating a fixpoint is not built, so past
    ``MAX_EXPANSION_DEPTH`` this raises rather than hangs.
    """
    if depth >= MAX_EXPANSION_DEPTH:
        raise SchemaError(
            f"{literal.predicate!r}: derived-predicate expansion exceeded "
            f"{MAX_EXPANSION_DEPTH} levels. Recursive predicates are expressible but "
            "not evaluated in v1 (FR10, §5.7)."
        )
    predicate = ctx.schema.predicate(literal.predicate).predicate
    for clause in predicate.clauses:
        substitution = {
            id(head_term): call_term
            for head_term, call_term in zip(clause.head, literal.terms, strict=True)
        }
        renamed = _rename(clause.body, substitution)
        # The caller sees only its own variables rebound: the clause's private
        # variables were renamed to fresh objects, so they cannot collide with a
        # variable of the same name anywhere else in the query.
        for solution in _solve(renamed.literals, bindings, ctx, depth=depth + 1):
            yield solution


def _rename(body: Conjunction, substitution: dict[int, object]) -> Conjunction:
    """Substitute head terms and rename every other variable apart."""
    fresh: dict[int, object] = {}

    def swap(term: object) -> object:
        if not isinstance(term, Var):
            return term
        if id(term) in substitution:
            return substitution[id(term)]
        if id(term) not in fresh:
            fresh[id(term)] = Prov() if isinstance(term, Prov) else Var(term.type)
        return fresh[id(term)]

    return Conjunction(
        literals=tuple(
            dataclasses.replace(
                literal,
                terms=tuple(swap(t) for t in literal.terms),
                at=None if literal.at is None else swap(literal.at),
            )
            for literal in body
        )
    )


# ---- terms ------------------------------------------------------------


def _resolve(term: object, bindings: Bindings) -> object:
    if isinstance(term, Var):
        return bindings.get(id(term), _UNBOUND)
    return term


def _bind(bindings: Bindings, term: object, value: object) -> Bindings | None:
    """Extend *bindings* so *term* holds *value*, or ``None`` if that contradicts."""
    if isinstance(term, Var):
        held = bindings.get(id(term), _UNBOUND)
        if held is _UNBOUND:
            return {**bindings, id(term): value}
        return bindings if held == value else None
    return bindings if term == value else None


def _ref_filter(value: object) -> EntityRef | None:
    return value if isinstance(value, EntityRef) else None


def _impossible(value: object, as_ref: EntityRef | None) -> bool:
    """A term bound to something that is not an entity ref can never match one."""
    return value is not _UNBOUND and as_ref is None
