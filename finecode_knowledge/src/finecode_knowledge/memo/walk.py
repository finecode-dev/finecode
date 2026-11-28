"""The verification walk and early cutoff (§4.3, §4.4, R3, R4, R22, ADR-0027).

``goals.md`` §4.3, step by step, is this module:

1. If ``node.verified_at == R``, return the memoized value. **O(1)** -- the common
   case in an editor loop, where most queries are unaffected by the edit.
2. Otherwise verify each recorded dependency, recursively.
3. If every dependency's change revision is at or below this node's
   ``verified_at``, nothing it read has actually changed: set ``verified_at = R``
   and return the memoized value **without recomputing**.
4. Otherwise recompute. Compare; advance ``changed_at`` **only** on a real
   difference. Either way set ``verified_at = R``.

Step 4's comparison is the whole point. Without it, one edit invalidates
everything transitively reachable and G4 holds only for the extraction layer.

## Which comparison, for which node (ADR-0027 D4)

§4.4 describes one cutoff; there are two, and reading §4.4 alone will give the
wrong answer for half the nodes.

- **Every node** is unconfirmed if an attributed bucket's *inputs* moved.
- **An insensitive node** may additionally cut off on the **unit digest**: a
  bucket that re-extracted to an identical fact multiset does not force it to
  recompute. This is §4.4's early cutoff, and it is sound for these nodes exactly
  because their values cannot contain anything the digest omits.
- **A sensitive node** -- one whose head carries a ``Prov``, so its value can
  contain a ``SourceLoc`` -- does not get that strengthening. Its attributed
  bucket changed inputs, so it recomputes, and the re-emitted violation carries
  the new line.

That is why an extraction node records **two** revisions of change:
``changed_at`` moves when its digest moves, ``input_changed_at`` when its inputs
move at all. Step 3 reads whichever one the dependent is entitled to.

## Verify-then-execute

ADR-0013 D6.3, and it falls out of the ordering rather than being enforced: steps
2 and 3 -- the awaiting, cancellable work -- run to completion before step 4 calls
the interpreter, whose walk is synchronous and deliberately uncancellable. The
long work is sequenced ahead of the short work, not interleaved with it.

## R15

A memo hit still unwinds. Step 1 returns without *looking*, which is precisely
where the ability to explain an answer is easiest to lose -- so the footprint and
the attributed buckets stay on the node across hits, and ``provenance_of`` reads
them back. An answer nobody can trace is not an answer this system is allowed to
give.

## The two read modes (ADR-0014 D6, §4.12)

The four steps above are ``Mode.VERIFIED``: block until the value is confirmed
current. ``Mode.CACHED`` short-circuits **after step 1** -- if a memoized value
exists it comes back immediately, carrying a ``CACHED`` reservation that says it
was not re-verified.

That the short-circuit sits before step 2 rather than after step 3 is the mode's
entire content. Steps 2 and 3 are the awaiting, potentially ER-dispatching half;
a cached read that waited on them would be a verified read with extra words.
Serving early is also why ``CACHED`` is a reservation and not a footnote: §4.12
allows staleness precisely because G3 forbids *silent* staleness, so the answer
has to carry the admission.

The mode is **not** part of the node key (``memo/keys.py``), so a cached-mode
read hits whatever the last verified pass computed. Recomputation therefore
always runs at ``Mode.VERIFIED``: a value either mode may be handed must not have
been produced under the weaker contract.

## Concurrency: cancellation, not MVCC (§4.11, R25, R13)

The WM is one event loop, so a walk that never awaits is atomic with respect to
every other task and gets its consistent snapshot for free. Only the awaiting
half can observe a change landing mid-flight, and there it is handled by Salsa's
answer rather than by keeping two worlds live: the walk pins the revision it
started at, and if the counter moved while it was suspended it raises
``Cancelled`` and **restarts at the new revision, reusing every memo entry still
valid there**. A query therefore always *appears* snapshot-consistent -- it
finished before the change or it started again after it -- which is how R25
delivers R13 rather than merely coexisting with it.

The partial result is discarded rather than written. It was computed against a
world that no longer exists, and a node recording it would claim a verification
at a revision nothing checked.

**Restarts are bounded by changes, not by walks.** Each restart needs its own
revision advance to have happened, so a walk terminates as soon as edits stop;
what keeps that from being a live-lock in practice is upstream and stated in
§4.11 -- the watcher debounces (concept.md §2) and freshness is per-save rather
than per-keystroke (§6). Nothing here caps the loop, deliberately: a cap would
turn "the world keeps moving" into a wrong answer instead of a slow one.

**Dedupe** is the other half §4.11 asks for. A node whose recomputation is
already in flight has a future in ``_inflight``; a second query needing the same
node awaits that future instead of launching a duplicate execution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import typing

from finecode_knowledge.memo.digest import unit_digest
from finecode_knowledge.memo.keys import (
    extraction_key,
    query_key,
    query_location_sensitive,
)
from finecode_knowledge.memo.node import (
    INITIAL_REVISION,
    MemoNode,
    NodeKey,
    NodeKind,
    Revision,
)
from finecode_knowledge.query import attribution
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.freshness import memo_not_verified

if typing.TYPE_CHECKING:
    from finecode_knowledge.memo.table import MemoTable
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.model.store import FactStore
    from finecode_knowledge.model.unit import BucketKey
    from finecode_knowledge.query.footprint import AccessKey
    from finecode_knowledge.query.query import Query, Result

__all__ = ["Cancelled", "Execute", "MemoWalk", "Provenance", "Refresh", "WalkStats"]


class Cancelled(Exception):
    """The revision moved while an awaiting walk was suspended (§4.11, R25).

    **Internal to the walk.** ``answer`` catches it and restarts, so no caller
    ever sees one -- it is control flow for "the world you pinned is gone", not a
    failure. It is a named exception rather than a sentinel return so that it
    cannot be dropped by an intermediate frame that forgot to check.
    """

    def __init__(self, pinned: Revision, current: Revision) -> None:
        super().__init__(
            f"the memo revision moved from {pinned} to {current} while a walk was "
            "in flight; restarting at the new one"
        )
        self.pinned = pinned
        self.current = current


Execute = "Callable[[Query, Mode, int | None], Awaitable[tuple[Result, tuple[AccessKey, ...]]]]"
"""What the walk calls when it has to recompute: run the query, and say what it read."""

Refresh = "Callable[[BucketKey], Awaitable[None]]"
"""What the walk calls, if anything, to fix a dirty bucket before comparing it.

**Optional, and the engine never looks inside it.** ``None`` is the default,
and then a dirty bucket is merely re-read from whatever the store currently
holds -- enough for a store somebody else keeps current, and the mode an
embedder with no extractor gets. When supplied, this is called with the dirty
bucket's key and is expected to have mutated the store in place by the time it
returns, or to have left it alone if it could not act; the walk never inspects
what changed, only whether the revision moved while it was awaiting.

A bare callable rather than a protocol naming an extractor, a process or an
action, because R20 keeps tool-specific logic out of the core: the engine must
not be able to name what fixes a bucket, only that something can."""


@dataclasses.dataclass
class WalkStats:
    """What the walk did, so R22 and R2 are assertable on **node visits**.

    Wall time would measure the machine; these count the mechanism. "An
    unaffected query returns in O(1)" and "a change event computes nothing" are
    both statements about these numbers, and neither is observable in the rows
    returned.
    """

    hits: int = 0
    """Step 1: served at the current revision without looking at a dependency."""
    verified_without_recompute: int = 0
    """Step 3: dependencies checked, nothing had changed, memoized value served."""
    recomputes: int = 0
    """Step 4: the query actually ran."""
    node_visits: int = 0
    """Every node the walk touched, including dependencies."""
    digest_cutoffs: int = 0
    """Extraction nodes that re-read to an identical fact multiset (§4.4)."""
    cached_serves: int = 0
    """``Mode.CACHED`` reads served without verifying (ADR-0014 D6, §4.12).

    Separate from ``hits``: a hit *was* verified at this revision and carries no
    reservation, a cached serve was not and does. Collapsing them would make the
    one number that distinguishes "fast and current" from "fast and admittedly
    stale" unreadable."""
    restarts: int = 0
    """Walks abandoned because the revision moved under them (§4.11, R25)."""
    deduped: int = 0
    """Recomputations that awaited one already in flight rather than launching a
    second (§4.11's in-flight map). Counts both kinds of in-flight join this
    walk does: a query recompute joining another query's, and a bucket refresh
    joining another already dispatched for the same key -- the same mechanism
    at the two granularities the walk awaits at, so one counter for both rather
    than a second that would only ever duplicate what this one already says."""
    refreshes: int = 0
    """Dirty buckets for which ``refresh`` was actually awaited (not joined --
    that is ``deduped``). Zero for a walk with no refresher, or one that never
    met a dirty bucket, which is most of them by design. This is what makes
    "only the changed parts were re-extracted" an assertable claim rather than
    an inference from timing."""

    def reset(self) -> None:
        self.hits = 0
        self.verified_without_recompute = 0
        self.recomputes = 0
        self.node_visits = 0
        self.digest_cutoffs = 0
        self.cached_serves = 0
        self.restarts = 0
        self.deduped = 0
        self.refreshes = 0


@dataclasses.dataclass(frozen=True)
class Provenance:
    """How an answer came to be -- served from a memo hit as readily as fresh (R15)."""

    node_key: NodeKey
    verified_at: Revision
    changed_at: Revision
    footprint: tuple[AccessKey, ...]
    buckets: tuple[BucketKey, ...]
    location_sensitive: bool


class MemoWalk:
    """The DAG, walked. One per store; holds no per-query state."""

    def __init__(
        self,
        table: MemoTable,
        store: FactStore,
        schema: SchemaRegistry,
        execute: Execute,  # type: ignore[valid-type]
        refresh: Refresh | None = None,  # type: ignore[valid-type]
    ) -> None:
        self._table = table
        self._store = store
        self._schema = schema
        self._execute = execute
        self._refresh = refresh
        """How to fix a dirty bucket, or ``None`` to re-read whatever the store
        already holds. The engine constructs no notion of what this is beyond
        "an awaitable that may have mutated the store" -- see ``Refresh``."""
        self.stats = WalkStats()
        self._inflight: dict[
            NodeKey, asyncio.Future[tuple[Result, tuple[AccessKey, ...]]]
        ] = {}
        """Recomputations currently awaiting, keyed by node (§4.11's dedupe map).

        Emptied in a ``finally``, so a failed execution leaves nothing for the
        next caller to await -- a stuck entry here would be a permanent hang for
        one question rather than an error anybody could see.
        """
        self._refresh_inflight: dict[BucketKey, asyncio.Future[None]] = {}
        """Bucket refreshes currently awaiting: two concurrent walks needing the
        same dirty bucket join one refresh rather than dispatching two. Keyed by
        bucket rather than by node key for the same reason ``_verify_extraction``
        is -- several node kinds can depend on one bucket, but there is exactly
        one thing to re-run for it."""

    # ---- the entry point ----------------------------------------------

    async def answer(
        self, query: Query, *, mode: Mode = Mode.VERIFIED, limit: int | None = None
    ) -> Result[list[tuple[object, ...]]]:
        """§4.3's four steps, for one query, restarted if the world moves under it.

        The loop is §4.11's cancel-and-restart. Each pass through it required a
        revision advance to have landed while this walk was suspended, so it
        terminates when edits stop; nothing caps it, because a cap would answer a
        moving world with a stale value rather than with a slower one.
        """
        while True:
            try:
                return await self._attempt(query, mode, limit)
            except Cancelled:
                self.stats.restarts += 1

    async def _attempt(
        self, query: Query, mode: Mode, limit: int | None
    ) -> Result[list[tuple[object, ...]]]:
        """One pass at §4.3's four steps, against one pinned revision."""
        revision = self._table.revision
        key = query_key(query, self._schema, limit=limit)
        node = self._table.node(
            key,
            NodeKind.QUERY,
            location_sensitive=query_location_sensitive(query, self._schema),
        )
        self.stats.node_visits += 1

        # Step 1.
        if node.verified_at == revision and node.servable:
            self.stats.hits += 1
            return typing.cast("Result[list[tuple[object, ...]]]", node.value)

        # ADR-0014 D6 / §4.12. Before step 2 rather than after step 3: steps 2
        # and 3 are the awaiting half, and a cached read that waited on them
        # would be a verified read with extra words. Reaching here means step 1
        # failed, so this value is by construction not verified at `revision` --
        # which is exactly the condition the reservation states.
        if mode is Mode.CACHED and node.servable:
            self.stats.cached_serves += 1
            return _reserved_cached(node, revision)

        # Step 2. Awaiting, cancellable work first (ADR-0013 D6.3).
        threshold = await self._verify_dependencies(node, revision)

        # Step 3.
        if node.servable and node.depends_on and threshold <= node.verified_at:
            node.verified(revision)
            self.stats.verified_without_recompute += 1
            return typing.cast("Result[list[tuple[object, ...]]]", node.value)

        # Step 4.
        return await self._recompute(node, query, limit, revision)

    def provenance_of(
        self, query: Query, *, limit: int | None = None
    ) -> Provenance | None:
        """What produced this query's memoized answer, if there is one (R15).

        Reads the node rather than re-deriving anything, which is the point: a
        memo hit returns without looking at its inputs, so the ability to explain
        it has to survive on the node itself.

        Takes no mode: since Phase 5 both modes read the same node, so "which
        facts produced this answer" has one answer regardless of how it was
        asked for.
        """
        node = self._table.get(query_key(query, self._schema, limit=limit))
        if node is None or not node.has_value:
            return None
        return Provenance(
            node_key=node.key,
            verified_at=node.verified_at,
            changed_at=node.changed_at,
            footprint=node.footprint,
            buckets=tuple(
                typing.cast("BucketKey", (dep[1], dep[2])) for dep in node.depends_on
            ),
            location_sensitive=node.location_sensitive,
        )

    # ---- step 2 -------------------------------------------------------

    async def _verify_dependencies(
        self, node: MemoNode, revision: Revision
    ) -> Revision:
        """Verify each recorded dependency; return the change revision that governs.

        Which change revision *is* ADR-0027 D4: an insensitive node reads each
        dependency's ``changed_at`` (the digest moved), a sensitive one reads
        ``input_changed_at`` (the inputs moved at all). One line, two cut
        strengths, and the classification was decided before the node ran.

        **Async because a dirty dependency may be refreshed before it is
        compared** (``_verify_extraction``), and a refresh is exactly the
        awaiting, cancellable work ADR-0013 D6.3 already sequences ahead of the
        interpreter -- this loop is where that await lives.
        """
        governing = INITIAL_REVISION
        for dep_key in node.depends_on:
            bucket = typing.cast("BucketKey", (dep_key[1], dep_key[2]))
            dependency = await self._verify_extraction(bucket, revision)
            moved = (
                dependency.input_changed_at
                if node.location_sensitive
                else dependency.changed_at
            )
            governing = max(governing, moved)
        return governing

    async def _verify_extraction(
        self, bucket: BucketKey, revision: Revision
    ) -> MemoNode:
        """Verify one fact bucket, refreshing it first if it is dirty and can be.

        An extraction node's own recompute is a comparison, not a production:
        it digests what the store holds for this bucket and lets §4.4's
        output-side cutoff decide whether anything above it must move. A
        refresher, when one is injected, is what can make that comparison see
        something new -- it is awaited first, and the cutoff then runs against
        whatever it left behind.

        The comparison does not move when a refresher is absent. A bucket with
        no refresher, or a dirty one a refresher declined to act on, falls
        straight through to the re-read: an embedder whose store is kept
        current by something outside the engine gets the same semantics for
        free, which is why the refresher is optional rather than required.
        """
        node = self._table.node(extraction_key(bucket), NodeKind.EXTRACTION)
        self.stats.node_visits += 1

        if node.verified_at == revision:
            return node

        if node.has_value and not self._table.is_dirty(bucket):
            # Nothing said this bucket moved, so there is nothing to compare.
            node.verified(revision)
            return node

        if self._refresh is not None and self._table.is_dirty(bucket):
            await self._refresh_bucket(bucket)
            # The only await this function adds is the one just above, so this
            # is the one place a change can have landed under it. A refresh
            # must not itself move the revision -- it only consumes the dirty
            # mark that triggered it, and a refresh that advanced the counter
            # would cancel the very walk that asked for it, once per bucket.
            # A revision that moved anyway means something else landed while
            # this walk was suspended, and the discard-and-restart `_recompute`
            # does for its own await applies here identically (§4.11, R25).
            current = self._table.revision
            if current != revision:
                raise Cancelled(revision, current)

        self._table.clear_dirty(bucket)
        digest = unit_digest(self._store.bucket(*bucket))
        first_sight = not node.has_value

        if not first_sight:
            # Its inputs moved: that is what put it on the dirty list. Sensitive
            # dependents recompute on this alone (ADR-0027 D4).
            node.input_changed_at = revision

        if not first_sight and node.digest == digest:
            # §4.4's early cutoff: the source changed, the facts did not.
            self.stats.digest_cutoffs += 1
            node.verified(revision)
            return node

        node.digest = digest
        node.value = digest
        node.has_value = True
        if first_sight:
            node.verified(revision)
        else:
            node.changed(revision)
        return node

    async def _refresh_bucket(self, bucket: BucketKey) -> None:
        """Await the injected refresher for *bucket*, deduped (§4.11, acceptance 4).

        Mirrors ``_execute_once``'s in-flight map exactly, one level down: two
        walks needing the same dirty bucket at once (two concurrent
        ``audit_code`` runs, say) share one ER round trip rather than each
        dispatching its own. A failed refresh reaches every waiter, the same
        way a failed query execution does -- there is nothing for a follower
        to fall back to that would not be silently pretending the refresh
        happened.
        """
        pending = self._refresh_inflight.get(bucket)
        if pending is not None:
            self.stats.deduped += 1
            await pending
            return

        assert self._refresh is not None  # only called when it is not
        self.stats.refreshes += 1
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._refresh_inflight[bucket] = future
        try:
            await self._refresh(bucket)
        except BaseException as error:
            future.set_exception(error)
            future.exception()
            raise
        else:
            future.set_result(None)
        finally:
            del self._refresh_inflight[bucket]

    # ---- step 4 -------------------------------------------------------

    async def _recompute(
        self,
        node: MemoNode,
        query: Query,
        limit: int | None,
        revision: Revision,
    ) -> Result[list[tuple[object, ...]]]:
        """Run the query, record what it read, and advance ``changed_at`` only on a difference."""
        result, footprint = await self._execute_once(node, query, limit)

        # §4.11. The only await in the walk has just returned, so this is the one
        # place a change can have landed underneath us. Nothing has been written
        # to the node yet, which is what makes discarding cheap and correct.
        current = self._table.revision
        if current != revision:
            raise Cancelled(revision, current)

        node.footprint = footprint
        node.depends_on = self._attribute(footprint)
        # Verify the dependencies the execution actually reached, *after* it ran.
        # Step 2 could only check the ones a previous run recorded, and a first run
        # records none -- so without this an extraction node would first exist at
        # some later revision and report "never changed" for a bucket that had
        # moved in between, and step 3 would serve a stale answer over it. Creating
        # them here pins each bucket's digest to the state this execution read.
        for dep_key in node.depends_on:
            await self._verify_extraction(
                typing.cast("BucketKey", (dep_key[1], dep_key[2])), revision
            )

        previous = node.value if node.has_value else None
        node.value = result
        node.has_value = True

        if previous is not None and _same_answer(previous, result):
            # The inputs moved but the answer did not. Leaving `changed_at` alone
            # is what stops the change here rather than propagating it to whatever
            # reads this node -- §4.4, one layer above extraction.
            node.verified(revision)
        else:
            node.changed(revision)
        return result

    async def _execute_once(
        self, node: MemoNode, query: Query, limit: int | None
    ) -> tuple[Result, tuple[AccessKey, ...]]:
        """Run the query, or join the run already in flight for this node (§4.11).

        The dedupe map coalesces concurrent consumers of one recomputation: an
        LSP pass and an agent loop asking the same question at the same revision
        pay for one execution, not two. Without it every consumer of a slow
        extraction launches its own, which is the load pattern §4.11 names.

        Execution is pinned to ``Mode.VERIFIED``. The mode is not in the node key,
        so this value may be served to either mode, and one produced under the
        weaker contract would let a cached read contaminate a verified one.
        """
        pending = self._inflight.get(node.key)
        if pending is not None:
            self.stats.deduped += 1
            return await pending

        self.stats.recomputes += 1
        future: asyncio.Future[tuple[Result, tuple[AccessKey, ...]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._inflight[node.key] = future
        try:
            outcome = await self._execute(query, Mode.VERIFIED, limit)
        except BaseException as error:
            future.set_exception(error)
            future.exception()
            # Read back so asyncio does not log an "exception was never
            # retrieved" for a future that may legitimately have no waiters --
            # the raise below is what the caller actually sees.
            raise
        else:
            future.set_result(outcome)
            return outcome
        finally:
            del self._inflight[node.key]

    def _attribute(self, footprint: tuple[AccessKey, ...]) -> tuple[NodeKey, ...]:
        """Which buckets could have answered this footprint (R11, ``query/attribution.py``).

        Static, from the schema, never from the rows -- because a scan that came
        back empty depends on the buckets that might have filled it. Narrowing to
        what was returned is silent in exactly the direction that stops a real
        violation being reported.

        Provider-granular on the way in and bucket-granular on the way out: the
        attribution maps a key to *providers*, so every unit of an attributed
        provider is a dependency. That over-approximates, which is the acceptable
        direction, and it is the same coarseness ``interpret._input_reservations``
        already applies to reservations.
        """
        providers = attribution.providers_for_footprint(footprint, self._schema)
        return tuple(
            extraction_key(unit.key)
            for unit in self._store.units()
            if unit.provider_id in providers
        )


def _reserved_cached(
    node: MemoNode, revision: Revision
) -> Result[list[tuple[object, ...]]]:
    """The memoized value, plus the admission that it was not re-verified (D6).

    The stored ``Result`` is left untouched and a copy carries the reservation:
    the node's value is what a later *verified* pass will compare against for
    early cutoff (§4.4), and folding a serving-time verdict into it would make a
    cached read look like the answer had changed.
    """
    result = typing.cast("Result[list[tuple[object, ...]]]", node.value)
    return dataclasses.replace(
        result,
        freshness=result.freshness.with_reservations(
            memo_not_verified(node.key, node.verified_at, revision)
        ),
    )


def _same_answer(previous: object, current: object) -> bool:
    """Whether recomputation produced the same value (§4.3 step 4).

    Compares **rows only**. A ``Result`` also carries a freshness verdict, and
    including it would mean a reservation appearing or clearing counted as the
    answer changing -- which would propagate a recomputation to every dependent
    over a change in how confident we are, not in what is true. The verdict is
    recomputed and returned either way; it just does not gate the cutoff.
    """
    previous_rows = getattr(previous, "value", previous)
    current_rows = getattr(current, "value", current)
    return previous_rows == current_rows
