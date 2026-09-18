"""The read seam the direct interpreter binds to (ADR-0013, ADR-0014 D4/D5).

``FactSource`` is **the direct interpreter's private fact source**, not the
system's storage abstraction. It has exactly one production implementation,
``FactStore``, and that is by design rather than by current circumstance::

    Query
      |- InterpreterBackend  -> FactSource   <- this module: in-process, one implementation
      |- CypherBackend       -> FalkorDB     <- whole-query compilation; no FactSource involved

**A graph engine must not implement it.** Driving one through a per-literal read
protocol costs a network round-trip per literal per binding *and* forgoes the
query planner that is the only reason to adopt it -- strictly worse than both
real options. Retargeting (NFR4) lives at ``Query`` -> dialect, and asynchrony
attaches at the ``Backend`` boundary where the network actually is. Neither
belongs here.

The protocol is **synchronous** (ADR-0013 D2). Every implementation is
in-memory by construction, so there is nothing to await: an ``async def`` that
never awaits allocates a coroutine per call, per literal, per row and holds the
event loop for exactly as long as before. Sync is also the *reversible* choice
-- a synchronous, I/O-free walk over an immutable snapshot can be moved off the
loop wholesale with ``asyncio.to_thread`` if execution ever stops being short,
which an async protocol could not be.

This module was named ``store_protocol.Store``. The rename is not cosmetic: the
old name made a read-only, interpreter-private, in-process interface read as the
system's storage boundary, and its docstring had drifted into describing a
WM-query-API-backed implementation that R13b forbids outright.
"""

from __future__ import annotations

import dataclasses
import typing

from finecode_knowledge.model.entity_type import EntityRef
from finecode_knowledge.model.facts import Provenance

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from finecode_knowledge.model.facts import EdgeFact, FieldFact

__all__ = ["Conflict", "FactSource", "FieldValue", "Record", "Revision"]

Revision = typing.NewType("Revision", str)
"""Opaque, comparable, stable identifier for exactly the fact content served.

For a file-loaded ``FactStore`` it is the fact file's content digest; on the
server path it is the WM's revision counter. The interpreter never interprets
it -- it only records it alongside the footprint (ADR-0013 D6.2)."""


@dataclasses.dataclass(frozen=True)
class FieldValue:
    value: object
    prov: Provenance


@dataclasses.dataclass(frozen=True)
class Conflict:
    """Two or more providers asserting different values for one field slot (C9).

    Per-value provenance is what makes a conflict actionable: it names which
    providers disagree and where each spoke.
    """

    entity: EntityRef
    field: str
    values: tuple[FieldValue, ...]
    """At least two, each carrying its own provenance."""


@dataclasses.dataclass(frozen=True)
class Record:
    """A whole entity's fields, plus whatever about it is contested (ADR-0014 D5).

    ``record()`` used to raise ``ConflictError`` instead. Carrying beats raising
    for the same reason ``goals.md`` §4.7 made dangling edges askable rather
    than refusable: an audit whose entire purpose is to report violations must
    not have its whole result destroyed by one contested field somewhere in the
    touched set, and a conflict is a fact *about the store* rather than a
    failure of the query -- precisely an input that cannot be confirmed, which
    is what a reservation is for.
    """

    fields: Mapping[str, FieldValue]
    conflicts: tuple[Conflict, ...] = ()


class FactSource(typing.Protocol):
    """Seven members, three of them lazy scans. ``FactStore`` satisfies it structurally.

    ``edge_facts`` and ``field_facts`` **return whole facts**, not bare refs.
    That is forced by FR5, not chosen for tidiness: the moment provenance is a
    bindable term (``Rel.includes_preset(a, b, at=prov)``), a ref-returning read
    costs a second lookup per row to recover ``at=`` -- a read that scales with
    result size, which is exactly what FR5 forbids. The four
    methods they replace (``targets_of``, ``sources_of``, ``find_by_field``,
    ``resolve``) each threw that provenance away.

    A field is addressed by ``(entity_type, field)``, never by field name alone:
    ``name`` exists on ``Package``, ``Handler`` and ``Environment``, so the pair
    is the identity.

    **The scans are lazy, and that is what deletes the existence primitives.**
    ``has_edge(kind, src, dst)`` and ``exists(kind, dst=...)`` are
    ``next(scan, None) is not None`` at O(degree) once the index exists, so
    adding them would be a second spelling of a question the scan already
    answers. Only ``contains`` survives, because "does this entity have any
    facts at all" is neither an edge nor a field scan.

    ``record`` and ``entities_of_type`` serve something other than the
    interpreter: under Datalog range restriction every variable is bound by a
    positive literal, so the interpreter never enumerates a type's population
    and never materializes a record. They exist for whole-store audits and
    entity materialization -- ``key_uniqueness_audit`` and ``which_handlers``
    -- and are kept deliberately rather than dropped and then smuggled back in
    by reaching past the seam for the concrete ``FactStore``, which would
    breach R21.

    **A contract an implementation must honour** (ADR-0013 D6):

    1. Immutable for the duration of an execution -- the interpreter never sees
       facts appear or vanish mid-query. This is also what makes thread-offload
       safe.
    2. ``revision`` is stable, comparable, and identifies exactly the content
       served.
    3. Every fact served is already verified at ``revision``. Verification may
       await; execution may not. So the memo layer verifies first, then
       executes -- the one constraint this seam imposes upward.
    4. Set semantics, unspecified order. Callers needing determinism use
       ``.order_by(...)`` (NFR8), not scan order.

    There is deliberately no footprint concept here -- no ``record_access``, no
    collector parameter, no ambient state. Recording is the *interpreter's* job
    (ADR-0013 D5), which is what lets ``FactStore`` satisfy this protocol with
    no changes and what stops freshness correctness from depending on every
    backend author remembering to instrument every method.
    """

    @property
    def revision(self) -> Revision: ...

    def edge_facts(
        self, kind: str, *, src: EntityRef | None = None, dst: EntityRef | None = None
    ) -> Iterator[EdgeFact]: ...

    def field_facts(
        self,
        entity_type: str,
        field: str,
        *,
        entity: EntityRef | None = None,
        value: object | None = None,
    ) -> Iterator[FieldFact]: ...

    def conflicts(
        self, entity_type: str, field: str, *, entity: EntityRef | None = None
    ) -> Iterator[Conflict]: ...

    def record(self, ref: EntityRef) -> Record: ...

    def contains(self, entity_type: str, ref: EntityRef) -> bool: ...

    def entities_of_type(self, entity_type: str) -> Iterator[EntityRef]: ...
