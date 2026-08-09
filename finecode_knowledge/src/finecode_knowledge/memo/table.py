"""The node store and the revision counter (§4.2, R2).

**In-memory, this version.** R11 already persists what a cold start needs to be
incremental in *extraction*, which is the expensive part. Persisting
``verified_at``/``changed_at`` additionally needs the revision counter to be
durable and every memoized value to be serializable -- including ``Violation``s
and arbitrary projection results. So the table starts empty each process
lifetime, and the consequence is stated rather than hidden: **a one-shot CLI run
gets no memo hits at all.** ``Mode.CACHED`` is reachable only in a live WM (LSP,
MCP, agent loop), which is exactly where §4.12 and G7 say the latency matters.

## Invalidation computes nothing, and this is where that is enforced

§4.1 splits the mechanism in two, and the split is the design:

- **Invalidation is eager, cheap, event-driven.** It marks. Its cost is bounded
  by the size of the change.
- **Computation is lazy, demand-driven.** Nothing is recomputed until a query
  needs a value.

``invalidate`` therefore advances the counter and records *which buckets are
dirty*. It does not walk the graph, it does not look at dependents, and it does
not call a provider or a rule body. That is not an optimization -- a walk here
would make invalidation cost O(graph) per keystroke, which is the thing R2
exists to forbid.

Dependents need no marking because the counter is global: once the revision
advances, no node satisfies step 1 (``verified_at == R``), so every query
re-enters the walk and step 3 lets the unaffected ones through **without
recomputing**. Marking dependents eagerly would buy nothing and cost a traversal.
"""

from __future__ import annotations

import typing

from finecode_knowledge.memo.node import (
    FIRST_REVISION,
    MemoNode,
    NodeKey,
    NodeKind,
    Revision,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from finecode_knowledge.model.unit import BucketKey

__all__ = ["MemoTable"]


class MemoTable:
    """Nodes keyed by identity, plus the revision they are measured against."""

    def __init__(self) -> None:
        self._nodes: dict[NodeKey, MemoNode] = {}
        self._revision: Revision = FIRST_REVISION
        self._dirty: set[BucketKey] = set()
        """Buckets whose inputs are known to have moved since they were last
        verified. Cleared per bucket as the walk reaches it, not in bulk: a
        bucket nobody queries stays dirty, correctly, until somebody asks."""

    # ---- revisions ----------------------------------------------------

    @property
    def revision(self) -> Revision:
        return self._revision

    def invalidate(self, units: Iterable[BucketKey]) -> Revision:
        """Mark *units* changed and advance the revision. **Computes nothing** (R2).

        Returns the new revision. Advancing even when *units* is empty would make
        every node fail step 1 for no reason, so an empty change is a no-op --
        which is what lets a watcher forward a debounced batch that turned out to
        contain nothing without paying for it.

        The caller does not say *how* it learned the units changed, and this
        method could not act on it if it did. A watcher event and a
        cold-start fingerprint diff arrive here identically, which is what makes
        R12's "correctness never depends on the watcher" a property of the code
        rather than a promise.
        """
        changed = set(units)
        if not changed:
            return self._revision
        self._dirty |= changed
        self._revision += 1
        return self._revision

    def is_dirty(self, bucket: BucketKey) -> bool:
        return bucket in self._dirty

    def clear_dirty(self, bucket: BucketKey) -> None:
        """Forget that *bucket* was marked. Called by the walk once it has checked."""
        self._dirty.discard(bucket)

    @property
    def dirty(self) -> frozenset[BucketKey]:
        return frozenset(self._dirty)

    # ---- nodes --------------------------------------------------------

    def get(self, key: NodeKey) -> MemoNode | None:
        return self._nodes.get(key)

    def node(self, key: NodeKey, kind: NodeKind, **defaults: object) -> MemoNode:
        """The node at *key*, created at the initial revision if absent.

        A newly created node is **not** verified at the current revision. It has
        no value, so step 1 cannot serve it and step 3 has nothing to confirm --
        it falls to step 4 and computes, which is what a first query must do.
        """
        held = self._nodes.get(key)
        if held is None:
            held = MemoNode(key=key, kind=kind, **defaults)  # type: ignore[arg-type]
            self._nodes[key] = held
        return held

    def revisions_of(self, key: NodeKey) -> tuple[Revision, Revision] | None:
        """``(verified_at, changed_at)`` for *key*, or ``None`` if unknown.

        A read-only window for tests and diagnostics: asserting on the two
        revisions is how §4.4's early cutoff is checked at all, since a node that
        cut off and one that recomputed to an identical value return the same
        rows.
        """
        held = self._nodes.get(key)
        return None if held is None else (held.verified_at, held.changed_at)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, key: NodeKey) -> bool:
        return key in self._nodes

    def __iter__(self) -> Iterator[MemoNode]:
        return iter(self._nodes.values())

    def of_kind(self, kind: NodeKind) -> list[MemoNode]:
        return [node for node in self._nodes.values() if node.kind is kind]

    def clear(self) -> None:
        """Drop every node and every dirty mark, keeping the revision.

        The revision survives on purpose: it is a statement about the *world*,
        not about this table, and rewinding it would let a node verified at the
        old number look current against the new one.
        """
        self._nodes.clear()
        self._dirty.clear()

    def __repr__(self) -> str:
        return f"MemoTable({len(self._nodes)} nodes @ r{self._revision}, {len(self._dirty)} dirty)"
