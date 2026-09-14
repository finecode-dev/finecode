"""The WM's knowledge home: the loaded fact store and its confirmation verdict (R13b).

``goals.md`` §4.10 decides that the **WM owns the logical store and the memo
table**, and that "owns the store" is about authority rather than about which
process the fact bytes sit in.

**What it does now.** It loads the store, runs R11's confirmation walk, holds the
schema an ER handed it, and **executes queries** an ER sends over
``knowledge/query``. Footprint capture comes with that for free and structurally:
the interpreter owns the collector (ADR-0013 D5) and the interpreter runs here,
so R7/R21 -- the read channel is the only way to read -- stops being a convention
the ER could break and becomes a fact about which process holds the store.

**And it memoizes.** The store's owner owns the DAG, so the memo table
lives here beside it: a query is answered from a memoized value when the walk can
confirm nothing it read has changed, and executed when it cannot. Invalidation
and computation stay the two separate mechanisms §4.1 insists on -- ``invalidate``
marks and computes nothing; ``run_query`` computes and marks nothing.

**And the read mode is real.** ``Mode.CACHED`` returns the memoized value
immediately, carrying a ``CACHED`` reservation that says it was not re-verified;
``Mode.VERIFIED`` blocks on the walk (ADR-0014 D6, §4.12). Because the memo is
in-memory, that mode means something **only here**: a one-shot CLI run
starts with an empty table and has nothing to serve, so a live WM -- LSP, MCP, an
agent loop -- is the only place the fast path exists. That is exactly where §4.12
and G7 say the latency matters.

**Why module-level state.** ``WorkspaceContext`` is the WM's shared mutable
state, but it sits at the bottom of the layer stack and services
sit above it, so a field there typed as a service object would invert the
dependency the ``wm-layered`` contract enforces. The WM constructs exactly one
``WorkspaceContext`` per process (``context.py``), so a module-level singleton
holds precisely the same lifetime with the imports pointing the right way.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import pathlib
import time
import typing

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.errors import FactsNotExtractedError, InternalError
from finecode.wm_server.runner import knowledge_bridge
from finecode_knowledge.fact_file import read_facts, write_facts
from finecode_knowledge.memo.table import MemoTable
from finecode_knowledge.memo.walk import MemoWalk
from finecode_knowledge.model.registry import SchemaRegistry
from finecode_knowledge.model.unit import BucketKey, Unit
from finecode_knowledge.model.verify import (
    BucketVerdict,
    Verdict,
    VerifyReport,
    verify_inputs,
)
from finecode_knowledge.model.wire import fact_from_json
from finecode_knowledge.query.backend import Mode
from finecode_knowledge.query.interpret import InterpreterBackend
from finecode_knowledge.query.records import records_to_json, refs_from_json
from finecode_knowledge.query.serialize import query_from_json, result_to_json
from finecode_knowledge.query.snapshot import registry_from_json

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from finecode_knowledge.model.store import FactStore
    from finecode_knowledge.query.footprint import FootprintCollector

__all__ = [
    "DEFAULT_FACTS_PATH",
    "bucket_verdict",
    "facts_file_path",
    "fetch_records",
    "invalidate",
    "invalidate_changed_inputs",
    "last_footprint",
    "last_walk_stats",
    "load_store",
    "memo",
    "persist_pending",
    "register_schema",
    "reset",
    "revision",
    "run_query",
    "schema",
    "set_schema",
    "verify",
]

_EXTRACT_KNOWLEDGE_ACTION = "extract_knowledge"
"""The config alias ``extract_knowledge`` is registered under
(``finecode_dev_common_preset/preset.toml``). Duplicated rather than imported
from ``fine_knowledge`` for the same reason ``DEFAULT_FACTS_PATH`` is: the WM
must not import the schema package, so the name travels as configuration this
module already agrees with the preset on, not as a shared constant."""

_PERSIST_INTERVAL_SEC = 5.0
"""Minimum gap between fact-file writes a refresh triggers.

``write_facts`` is O(whole store) always (``fact_file.py`` has no incremental
form) and the file runs to tens of megabytes, so writing it after *every*
single-bucket refresh would pay a whole-store cost per bucket -- the very cost
refreshing one bucket at a time exists to avoid. Throttling to this interval batches however
many refreshes land in the window into one write, and bounds what a crash
between writes can lose to at most one interval's worth of refreshes -- each
cheaply re-derivable from source (R17), which is what the fingerprints this
same store carries are *for*."""

_INPUTS_MOVED = frozenset({Verdict.STALE, Verdict.MISSING})
"""Bucket verdicts that mean "this unit's sources are not what it was extracted from".

``UNTRACKED`` is deliberately absent. An untracked input is one nothing can
fingerprint (R9), so it says *we could not check*, not *it changed* -- treating it
as a change would advance the revision on every walk and disable the memo
entirely, for buckets that may well be current.
"""

DEFAULT_FACTS_PATH = ".finecode/knowledge/facts.json"
"""Where ``extract_knowledge`` writes, relative to the workspace root.

Duplicated rather than imported from ``fine_knowledge``: that package is the
FineCode *schema* half, which the WM must not import. The path travelling as
configuration instead of as a shared constant is the same answer the schema
itself gets: handed over as data, never imported.
"""


@dataclasses.dataclass
class _State:
    """Process-lifetime knowledge state. One per WM process."""

    schema: SchemaRegistry | None = None
    snapshot: dict | None = None
    """The wire form the current schema was rebuilt from, kept so a re-registration
    of the *same* schema is recognized as such and costs nothing."""
    store: FactStore | None = None
    report: VerifyReport | None = None
    report_key: tuple[int, tuple[int, int] | None] | None = None
    """The ``(memo revision, fact file (mtime_ns, size))`` pair ``report`` was
    computed at. Keyed to both rather than held until ``reset()``, which would
    outlive its validity the moment anything else advanced the revision: a
    report that no longer describes what a caller is about to read is
    recomputed rather than served, and pinning it to neither trigger in
    particular is what keeps that true however the change arrived."""
    loaded_from: pathlib.Path | None = None
    facts_stamp: tuple[int, int] | None = None
    """``(mtime_ns, size)`` of the fact file at the moment it was loaded, so a
    later read can notice an out-of-band ``extract_knowledge`` without having
    to re-read the file to find out."""
    walk_stats: object = None
    """What the most recent memo walk did. Diagnostics, and the only way a test
    can tell a memo hit from a recomputation that produced the same rows."""

    footprint: FootprintCollector | None = None
    """What the most recent execution read (ADR-0013 D5).

    Held rather than returned: a footprint is one key per access, the wrong size
    for a value whose job is to be read (ADR-0014 D3), and its consumer is the
    memo table -- which lives here too. It does **not** cross to the
    ER, and could not usefully: the ER does not read facts, so it has nothing to
    attribute a key to.
    """

    dirty_since_persist: bool = False
    """Whether ``_state.store`` holds a refreshed or retracted bucket
    the fact file does not yet reflect. Drives the throttled write in
    ``_maybe_persist`` and the unconditional one in ``persist_pending``."""
    last_persisted_at: float = 0.0
    """``time.monotonic()`` of the last fact-file write a refresh triggered.
    Compared against ``_PERSIST_INTERVAL_SEC`` to throttle; ``0.0`` (never
    persisted) is always due."""


_state = _State()
_memo = MemoTable()
"""The DAG, for this process's lifetime.

Beside the store rather than inside it, because a store is a value that tests
construct freely and the memo is process state. Both are held here for the same
reason ``goals.md`` §4.10 gives: the owner of the store owns the DAG.
"""


_refresh_inflight: dict[BucketKey, asyncio.Future[None]] = {}
"""Bucket refreshes in flight anywhere in this process, keyed by bucket.

**Per bucket, not per process.** Two refreshes of *different* buckets are the
normal case, not a recursion: one query walks several dirty buckets, and
several queries run at once whenever an action's handlers do. A single
process-wide flag cannot tell those apart from a genuine nested refresh, and
using one drops sibling refreshes wholesale -- the bucket stays dirty, the
answer still reserves it as stale, and the whole point of refreshing on demand
is lost while looking like it merely had nothing to do.

Deduping here rather than in ``MemoWalk`` because a walk is built per query:
its own in-flight map cannot see a refresh another query already dispatched
for the same bucket, and two queries needing one bucket should cost one round
trip."""

_MAX_CONCURRENT_REFRESHES = 4
"""How many bucket refreshes may be dispatched at once.

Each one is a whole action run, and nothing else bounds their number: a walk
verifies its dependencies sequentially, but concurrent queries each bring
their own. This queues the surplus rather than dropping it -- a dropped
refresh is a silently unfixed bucket, a queued one is just later."""

_REFRESH_JOIN_TIMEOUT_SEC = 60.0
"""How long to wait on a refresh of the same bucket already in flight.

A bound rather than an unbounded await, because the one shape that could hang
here is a provider whose own extraction reads facts attributed to the bucket
it is extracting: the nested call would wait for a dispatch that cannot finish
until it returns. That reaches this process as a fresh request on its own task,
so no in-process scoping can recognize it -- but timing out degrades it to a
stale read, which is the same outcome any other failed refresh gets."""

_loop_bound: dict[str, tuple[object, object]] = {}
"""Asyncio primitives, remembered with the loop they belong to.

A module-level ``Semaphore`` or ``Lock`` binds to the first loop that awaits
it. A process running one loop never notices; a test suite running several
does, as a wait that never wakes. Rebuilding on a loop change keeps both
honest without giving either a lifetime shorter than the state it guards.
"""


def _for_loop(name: str, make: typing.Callable[[], object]) -> typing.Any:
    loop = asyncio.get_running_loop()
    cached = _loop_bound.get(name)
    if cached is None or cached[0] is not loop:
        cached = (loop, make())
        _loop_bound[name] = cached
    return cached[1]


def _refresh_gate_for_loop() -> asyncio.Semaphore:
    """How many bucket refreshes may be dispatched at once."""
    return _for_loop(
        "refresh_gate", lambda: asyncio.Semaphore(_MAX_CONCURRENT_REFRESHES)
    )


def _store_lock_for_loop() -> asyncio.Lock:
    """Held while the store is mutated or written.

    Writing moved off the event loop (``_persist_now``), which bought back the
    ~1s stall a whole-store write costs -- and introduced the hazard that
    buying it back always does: the store is no longer frozen for the duration
    of its own serialization, so a refresh ingesting concurrently could mutate
    the very dict being walked. Ingestion is in-memory and brief, so holding
    the same lock across both is cheap and removes the window entirely.
    """
    return _for_loop("store_lock", asyncio.Lock)


def memo() -> MemoTable:
    """The memo table. Exposed so a caller can ask what it knows, not to mutate it."""
    return _memo


def revision() -> int:
    """The memo DAG's current revision (§4.2)."""
    return _memo.revision


def reset() -> None:
    """Drop the loaded store and its verdict, keeping the schema.

    **Marks every bucket the store held as changed**, because a reload is a
    change of *unknown* extent: the next read may return anything, and a memo
    that kept confirming nodes across it would be serving answers over facts it
    never saw. That is a blunt instrument on purpose -- the precise path is
    ``invalidate_changed_inputs``, which knows which buckets actually moved.

    Used by tests, and by anything that has to put the store back to unknown.
    """
    if _state.store is not None:
        invalidate(unit.key for unit in _state.store.units())
    _state.store = None
    _state.report = None
    _state.report_key = None
    _state.loaded_from = None
    _state.facts_stamp = None
    _state.footprint = None


def set_schema(registry: SchemaRegistry) -> None:
    """Install the registry the store validates against.

    The WM has to attribute footprint keys to buckets, which reads every
    provider's ``SUPPLIES`` lists -- but the schema and the providers are
    declared by extension packages the WM must not import. So it is *given* a
    registry rather than importing one, and a third-party schema participates on
    identical terms (R18/R19) because a handed-over registry does not care who
    declared it.

    ``register_schema`` calls this with the snapshot an ER sends at registration;
    a test may call it directly with a registry it built in-process.
    """
    if registry is _state.schema:
        return
    _state.schema = registry
    _state.snapshot = None
    reset()


def schema() -> SchemaRegistry | None:
    """The registry an ER handed over, or ``None`` if none has."""
    return _state.schema


def invalidate(units: Iterable[BucketKey]) -> int:
    """Mark *units* changed and advance the revision. **Computes nothing** (R2, §4.1).

    The one code path every trigger goes through, and that is the point: it
    takes "these units changed" and cannot tell -- and must not be able to tell --
    whether a file watcher or a cold-start fingerprint diff produced the list.
    R12's "correctness never depends on the watcher" is then a property of there
    being no second path, rather than a promise about how the paths are kept in
    step.

    Cost is bounded by the size of the change: no provider runs, no rule body
    executes, and the graph is not walked. Dependents need no marking because the
    revision is global -- advancing it is what makes every query re-enter the
    walk, where step 3 lets the unaffected ones through without recomputing.
    """
    return _memo.invalidate(units)


def invalidate_changed_inputs(ws_context: context.WorkspaceContext) -> int:
    """The cold-start trigger: invalidate whatever the confirmation walk says moved.

    The trigger that makes a watcher optional. A WM that started without one,
    or that missed events while it was not running, reaches exactly the same
    table state by comparing R11's persisted fingerprints against what is on
    disk now.

    **Always forces a fresh walk** (``verify(ws_context, force=True)``), rather
    than reusing ``verify()``'s own cache. That cache is keyed to ``(memo
    revision, fact-file stamp)`` precisely so a *settled* world costs nothing
    to re-ask about -- but this function's entire job is answering "did a
    *source* move", and neither of those two numbers changes when a tracked
    source is edited with no extraction run in between. A cache that assumes
    the answer to that question without checking would leave a plain source
    edit undiscoverable -- the one case an on-demand refresh most needs to
    catch, since no other signal reports it.

    Returns the resulting revision.
    """
    report = verify(ws_context, force=True)
    return invalidate(
        key for key, verdict in report.verdicts.items() if verdict.kind in _INPUTS_MOVED
    )


def last_walk_stats() -> object:
    """What the most recent walk did -- hits, skips, recomputes, node visits.

    Counting the mechanism rather than timing it is what makes R22 and R2
    assertable: a node that cut off and one that recomputed to an identical value
    return the same rows, so the answer cannot distinguish them.
    """
    return _state.walk_stats


def last_footprint() -> FootprintCollector | None:
    """The slots the most recent query execution consulted, or ``None`` if none ran.

    The memo table is this value's real consumer. It is also what makes R7/R21
    assertable -- the footprint exists *here*, in the process that did the
    reading, and nowhere else.
    """
    return _state.footprint


def facts_file_path(ws_context: context.WorkspaceContext) -> pathlib.Path:
    """Absolute path of the fact file for this workspace.

    Raises:
        InternalError: the workspace has no root directory, so there is nothing
            to resolve the relative path against.
    """
    workspace_root = context.pick_workspace_root_dir(ws_context)
    if workspace_root is None:
        raise InternalError(
            "Cannot locate the knowledge fact file: the workspace has no root directory. "
            "Add a workspace directory before reading facts."
        )
    return workspace_root / DEFAULT_FACTS_PATH


def _facts_stamp(path: pathlib.Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of *path*, or ``None`` if it does not exist.

    The same cheap gate R11's confirmation walk uses for a single tracked
    source (``model/verify.py``), applied to the fact file as a whole: enough
    to notice it was rewritten without reading it.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_store(ws_context: context.WorkspaceContext) -> FactStore:
    """The workspace's fact store, read from disk on first use and held after.

    Raises:
        FactsNotExtractedError: no fact file exists yet.
    """
    if _state.store is not None:
        return _state.store

    path = facts_file_path(ws_context)
    # KNOWN: this read is on the event loop, and it is not cheap -- measured
    # ~1.6s for a 30MB fact file, during which the WM answers nobody. The
    # write moved off the loop (`_persist_now`) because it repeats; this one
    # fires on first use and then only when the file moves out-of-band, which
    # is once per manual `extract_knowledge`, not once per refresh. Offloading
    # it means making `verify`, `bucket_verdict` and `invalidate_changed_inputs`
    # async, which is a wider change than the frequency currently justifies.
    # Revisit if a stall shows up right after a manual extraction.
    try:
        store = read_facts(_schema_for_loading(), path)
    except FileNotFoundError as error:
        raise FactsNotExtractedError(
            f"No knowledge facts at {path}. Run the 'extract_knowledge' action first."
        ) from error

    _state.store = store
    _state.loaded_from = path
    _state.facts_stamp = _facts_stamp(path)
    logger.debug(f"Loaded knowledge facts from {path} at revision {store.revision}")
    return store


def _reload_if_facts_moved(ws_context: context.WorkspaceContext) -> None:
    """Notice an out-of-band ``extract_knowledge`` before a current read.

    Compares the loaded store's fact file against its ``(mtime, size)`` at load
    time. A move means the store in memory predates the file's last write, so
    it is unknown-stale in exactly the sense ``reset()`` already handles and the
    store is dropped.

    **The blunt mark needs no narrowing.** ``reset()`` marks every bucket the
    old store held, and ``invalidate`` only ever *adds* to the dirty set --
    there is no un-marking. What keeps the cost bounded is one layer down: the
    walk visits each dirty extraction node and recomputes its *digest*, and
    §4.4's cutoff stops the ones whose facts did not actually move from
    advancing ``changed_at``. So a reload costs O(buckets) digests, not
    O(rules) re-executions.

    **This does not invalidate anything itself.** ``run_query`` calls
    ``invalidate_changed_inputs`` unconditionally right after this returns,
    and that already covers what this function detects -- a reloaded store
    whose every bucket is unconfirmed -- as a special case of the broader
    question of which buckets moved. Doing it here as well would walk twice
    for one edit.

    Called from ``run_query`` under ``Mode.VERIFIED`` only, and unconditionally
    from ``fetch_records`` (see its docstring for why) -- never under
    ``Mode.CACHED``, whose whole contract is answering without paying this
    cost (ADR-0014 D6).

    A no-op before the first load: nothing is loaded, so nothing could have
    moved out from under it.
    """
    if _state.store is None:
        return
    path = facts_file_path(ws_context)
    if _facts_stamp(path) == _state.facts_stamp:
        return
    reset()


def verify(
    ws_context: context.WorkspaceContext, *, force: bool = False
) -> VerifyReport:
    """Run R11's cold-start confirmation walk over the loaded store.

    No new verdict logic: ``model/verify.py``'s walk is called, so the WM's store
    reports itself exactly as the in-ER path already does. The walk reads each
    unit's declared inputs and stats them; it never consults the schema, which is
    what lets this land a phase before the registry snapshot does.

    The report is held alongside the store, keyed to the memo revision and the
    fact file's own ``(mtime, size)`` rather than to "since the last
    ``reset()``": either one moving means the report may no longer describe
    what a caller is about to read, and it must not outlive that. Asking twice
    with neither having moved costs one walk.

    ``force`` bypasses that cache-hit check and always re-walks --
    for ``invalidate_changed_inputs``, whose whole job is answering "did a
    source move" and cannot answer it from a cache keyed on two numbers that
    do not change when a source is edited with no extraction in between. Every
    other caller leaves it ``False`` and keeps the O(1)-when-settled behaviour
    this function was built for.
    """
    store = load_store(ws_context)
    key = (_memo.revision, _facts_stamp(facts_file_path(ws_context)))
    if not force and _state.report is not None and _state.report_key == key:
        return _state.report

    workspace_root = typing.cast(
        pathlib.Path, context.pick_workspace_root_dir(ws_context)
    )
    report = verify_inputs(store, workspace_root)
    _state.report = report
    _state.report_key = key
    unconfirmed = report.unconfirmed()
    if unconfirmed:
        logger.debug(
            f"{len(unconfirmed)} of {len(report.verdicts)} knowledge buckets are "
            f"unconfirmed after {report.stat_count} stats and {report.hash_count} hashes"
        )
    return report


def bucket_verdict(
    ws_context: context.WorkspaceContext, provider_id: str, unit_id: str
) -> BucketVerdict:
    """The verdict for one bucket.

    Raises:
        InternalError: the store holds no such bucket. A caller asking about a
            bucket that was never extracted has a stale key, which is a WM-state
            inconsistency rather than a user-facing condition.
    """
    report = verify(ws_context)
    key: BucketKey = (provider_id, unit_id)
    try:
        return report[key]
    except KeyError as error:
        raise InternalError(
            f"No knowledge bucket {key} in the store loaded from {_state.loaded_from}."
        ) from error


def _schema_for_loading() -> SchemaRegistry:
    """The registry a freshly read store is bound to.

    Reading a fact file does not consult the schema -- ``FactStore.from_json``
    rebuilds buckets straight from the wire form, bypassing the ``ingest``
    validation that would need one -- and neither does the confirmation walk. So
    an empty registry is sound for confirming buckets and for nothing beyond it,
    which is why ``run_query`` refuses rather than falling back to it: a query
    executed against an empty registry does not fail cleanly, it fails at the
    first entity type it cannot resolve.
    """
    if _state.schema is None:
        logger.debug(
            "No schema registry has been handed to the WM yet, so the fact store is "
            "loaded against an empty one -- enough to confirm buckets, not to query them."
        )
        return SchemaRegistry()
    return _state.schema


# ---- the ER-facing half (R13b, goals.md §4.10) -------------------------


async def register_schema(snapshot: dict) -> bool:
    """Install the schema an ER declared, as data -- never imported.

    Idempotent by value: two runners hosting the same schema send the same
    snapshot, and re-installing it would drop the loaded store and its verdict
    for nothing. Two *different* schemas are a real ambiguity and the last one
    wins loudly rather than silently -- there is nothing here that could merge
    them, and the WM has no basis to prefer either.

    Raises:
        SnapshotError: *snapshot* is not one this build can read. Raised rather
            than swallowed: an ER that thinks the WM holds its schema and finds
            it does not would fail later, at a query, with nothing pointing back
            at registration.
    """
    if _state.schema is not None and _state.snapshot == snapshot:
        return False

    registry = registry_from_json(snapshot)
    if _state.schema is not None:
        logger.warning(
            "A second, different knowledge schema was registered; the WM holds one "
            "store and reads it against the most recent schema."
        )
    set_schema(registry)
    _state.snapshot = snapshot
    logger.debug(
        f"Registered a knowledge schema with {len(registry.entity_types())} entity "
        f"types and {len(registry.providers())} providers"
    )
    return True


async def fetch_records(ws_context: context.WorkspaceContext, refs: list[dict]) -> dict:
    """Everything the store knows about *refs*, in one reply (R21).

    The one read that is not a query, and the reason it is here rather than in
    the ER: a projection rendering an entity's whole field set cannot name the
    fields, because R18/R19 keep that set open to packages the projection has
    never heard of. Before this existed, `which_handlers` held a `FactSource`
    and read it directly -- outside the channel, so contributing **no footprint
    key**, so invisible to invalidation.

    The footprint is captured here and held beside the query one, on the same
    terms: an ``("entity", ref)`` key attributes to every provider supplying that
    entity type, so a projection built from these records is invalidated exactly
    as a rule is. Nothing memoizes a projection yet (``NodeKind.PROJECTION``
    exists and nothing constructs one), so today the key is recorded and
    consulted by no node -- which is the right order: the read stops bypassing
    the channel *before* anything starts depending on it.

    Raises:
        FactsNotExtractedError: no fact file exists yet.
        InternalError: no ER has registered a schema.
        QueryWireError: *refs* is not a ref list this build can read.
    """
    registry = _state.schema
    if registry is None:
        raise InternalError(
            "No knowledge schema has been registered with this WM, so an entity "
            "reference cannot be resolved. The runner hosting the schema sends it "
            "with 'knowledge/registerSchema' before its first read."
        )

    # fetch_records has no Mode parameter -- unlike run_query, a caller cannot
    # opt out of re-verification the way Mode.CACHED lets it. Treated as always
    # current: this read renders a whole entity for something the caller is
    # about to act on (originally `which_handlers`), and there is no cheaper
    # variant to fall back to, so defaulting to "maybe stale" here would remove
    # a guarantee run_query still offers rather than just not extending it.
    _reload_if_facts_moved(ws_context)

    store = load_store(ws_context)
    backend = InterpreterBackend(store, schema=registry, verdicts=verify(ws_context))
    found = await backend.records(refs_from_json(refs))
    _state.footprint = backend.last_footprint
    return records_to_json(found)


async def run_query(
    ws_context: context.WorkspaceContext,
    query: dict,
    *,
    mode: str = Mode.VERIFIED.value,
    limit: int | None = None,
) -> dict:
    """Execute a serialized query against the WM's store. One query in, one result out.

    This is ``goals.md`` §4.10's access path, and the reason it is a *query* rather
    than a read: the unit of access is a whole question, so the boundary costs one
    round trip regardless of how many facts answering it touches, and a question
    reaching a derived relation is answerable at all -- derived facts are not in
    the store, they are computed here.

    The footprint is captured here and stays here (ADR-0013 D5, R7/R21). It is not
    in the result: its consumer is the memo table, which lives on this side too.

    Raises:
        FactsNotExtractedError: no fact file exists yet.
        InternalError: no ER has registered a schema, so there is nothing to
            resolve the query's names against.
        QueryWireError: *query* is not a serialized query this build can read.
    """
    registry = _state.schema
    if registry is None:
        raise InternalError(
            "No knowledge schema has been registered with this WM, so a query's "
            "entity types and predicates cannot be resolved. The runner hosting the "
            "schema sends it with 'knowledge/registerSchema' before its first query."
        )

    # Only a verified read pays to notice an out-of-band extraction: that is
    # ADR-0014 D6's whole reason for two modes, and Mode.CACHED's contract is
    # "answer now, tell me it was not re-verified" -- reloading the store here
    # would silently upgrade that promise into one CACHED does not make.
    #
    # Two calls, because they catch different things. `_reload_if_facts_moved`
    # only sees the fact *file* move (an out-of-band `extract_knowledge`); a
    # tracked *source* edited with nothing else happening moves neither the
    # memo revision nor the file's stamp, so no cheap signal reports it at all.
    # `invalidate_changed_inputs` forces the fingerprint walk that does, which
    # is what dirties a bucket before the walk below looks at it.
    #
    # That walk is the standing cost of a verified read: ~30ms over ~400
    # buckets on a workspace this size, almost all of it `stat` calls, and it
    # grows with the number of tracked files. Affordable for a CI gate or an
    # audit; an editor loop that cannot pay it asks under `Mode.CACHED`, which
    # never reaches this branch.
    if Mode(mode) is Mode.VERIFIED:
        _reload_if_facts_moved(ws_context)
        invalidate_changed_inputs(ws_context)

    store = load_store(ws_context)
    backend = InterpreterBackend(store, schema=registry, verdicts=verify(ws_context))

    async def execute(built, read_mode, row_limit):
        """Step 4's recompute. The only path on which the interpreter runs."""
        answered = await backend.run(built, mode=read_mode, limit=row_limit)
        _state.footprint = backend.last_footprint
        return answered, backend.last_footprint.keys

    # The walk is handed a refresher, never told what one is: `_refresh` is
    # the only thing here that knows "refresh" means "dispatch
    # extract_knowledge into an ER", and the engine must not be able to name
    # either (R20).
    walk = MemoWalk(
        _memo, store, registry, execute, refresh=functools.partial(_refresh, ws_context)
    )
    result = await walk.answer(
        query_from_json(query, registry), mode=Mode(mode), limit=limit
    )
    _state.walk_stats = walk.stats
    return result_to_json(result)


# ---- re-extraction on demand -------------------------------------------


async def _refresh(ws_context: context.WorkspaceContext, bucket: BucketKey) -> None:
    """Re-extract one bucket through the ER and ingest what comes back.

    Injected into ``MemoWalk`` as its ``refresh`` callable: the walk never
    learns this dispatches an action into an ER, only that it awaited
    something that may have mutated the store it already holds.

    **Never raises.** A refresh that cannot even be dispatched -- no workspace
    root project, no ``extract_knowledge`` action registered, which is the
    state every bare ``WorkspaceContext`` in this module's own test suite runs
    in -- or one that fails in flight (ER unreachable, action error, an
    unparsable result) leaves the bucket exactly where it was: still dirty,
    still reserved as stale by ``verify()``. A refresh was always a
    best-effort warming step (§4.1), never a correctness requirement, so a
    query must not fail because one could not run.
    """
    provider_id, unit_id = bucket
    workspace_root = context.pick_workspace_root_dir(ws_context)
    if workspace_root is None:
        return
    root_project = ws_context.ws_projects.get(workspace_root)
    if not isinstance(root_project, domain.CollectedProject):
        return
    if not any(
        action.name == _EXTRACT_KNOWLEDGE_ACTION for action in root_project.actions
    ):
        return

    joined = _refresh_inflight.get(bucket)
    if joined is not None:
        # Someone else is already re-extracting exactly this bucket. Wait for
        # their result instead of dispatching a second identical run -- but
        # under a bound, because the one caller that could be waiting on
        # itself (a provider reading facts of the bucket it is extracting)
        # arrives as a separate request on its own task and is
        # indistinguishable from here.
        try:
            await asyncio.wait_for(
                asyncio.shield(joined), timeout=_REFRESH_JOIN_TIMEOUT_SEC
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            logger.warning(
                f"Refresh of {provider_id}:{unit_id} joined one already in flight "
                "that did not finish in time; the bucket stays stale."
            )
        return

    # KNOWN, unproven: this dispatch targets the workspace root in the same
    # env the querying run is using, so the runner asked to re-extract can be
    # the very one blocked awaiting the query this refresh serves. Its
    # handlers call back to the WM, and a live run showed one such callback
    # timing out. If request handling serializes per connection anywhere in
    # that cycle, the WM waits on the ER while the ER waits on the WM, and no
    # in-process guard can see it -- the loop closes through two processes.
    # Not reproduced deliberately; suspected cause of an
    # `extract_knowledge` failure during `audit_code` on 2026-08-07.
    from finecode.wm_server.services import (  # noqa: PLC0415; avoid a module cycle
        run_service,
    )

    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _refresh_inflight[bucket] = done
    try:
        async with _refresh_gate_for_loop():
            try:
                executor = run_service.WorkspaceExecutor(ws_context)
                responses = await executor.run_actions_in_projects(
                    actions_by_project={workspace_root: [_EXTRACT_KNOWLEDGE_ACTION]},
                    params={
                        "buckets": [f"{provider_id}:{unit_id}"],
                        "return_facts": True,
                    },
                    run_trigger=run_service.RunActionTrigger.SYSTEM,
                    dev_env=run_service.DevEnv.CLI,
                    # Never zero, so the recursion-depth cap (`WorkspaceExecutor`,
                    # ADR-0095) counts this dispatch like any other nested one.
                    orchestration_depth=1,
                    # Nobody awaits this and its result is re-derivable, so a
                    # config reload cancels it rather than being refused by it
                    # (ADR-0080).
                    cancellable=True,
                    # Same reason it is cancellable: the WM started this on its
                    # own behalf, so there is nobody to ask.
                    origin=None,
                )
            except Exception as error:  # noqa: BLE001
                logger.debug(
                    f"Refresh of {provider_id}:{unit_id} did not complete: {error}"
                )
                return
    finally:
        _refresh_inflight.pop(bucket, None)
        if not done.done():
            done.set_result(None)

    response = responses.get(workspace_root, {}).get(_EXTRACT_KNOWLEDGE_ACTION)
    if response is None:
        return
    try:
        result = response.json()
    except Exception as error:  # noqa: BLE001
        logger.debug(
            f"Refresh of {provider_id}:{unit_id} returned no usable result: {error}"
        )
        return

    try:
        await _ingest_refresh_result(ws_context, bucket, result)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            f"Refresh of {provider_id}:{unit_id} fetched a result but could not "
            f"be ingested: {error}"
        )
        return

    # A completed refresh is a state change -- facts replaced or retracted, and
    # a fact-file write owed -- and it is bounded by what actually moved, so it
    # does not run in a loop. It is also the only evidence that re-extraction
    # is working at all: a refresh that never fires and one that fires and
    # changes nothing produce the same answer, so silence here made a broken
    # pull edge look exactly like an idle one.
    logger.info(f"Re-extracted {provider_id}:{unit_id} after its inputs changed")


async def _ingest_refresh_result(
    ws_context: context.WorkspaceContext, bucket: BucketKey, result: dict
) -> None:
    """Ingest a scoped ``extract_knowledge`` run's result into the WM's own store.

    The facts arrive in the action *result* (``payload.return_facts``) rather
    than through the fact file, which is what keeps a per-bucket refresh from
    costing O(whole store) each time.

    Retraction is decided here too: whether the requested bucket appears in
    ``result["buckets"]`` is the signal, and this is the one place that both
    sent the request and reads the response -- so it is the only place that
    can tell "asked and got nothing" apart from "was never asked about".
    """
    provider_id, unit_id = bucket
    requested_entry = f"{provider_id}:{unit_id}"
    rewritten = set(result.get("buckets", []))
    if requested_entry not in rewritten:
        await _retract(ws_context, bucket)
        return

    entries = {entry.get("bucket"): entry for entry in result.get("facts", [])}
    entry = entries.get(requested_entry)
    if entry is None:
        # `return_facts=True` should always carry the rewritten bucket's
        # facts (see the payload's own docstring); an ER built against an
        # older `fine_knowledge` that ignores the flag falls back to leaving
        # the bucket where it was, consistent with `_refresh`'s contract.
        return

    store = _state.store
    if store is None:
        return
    unit = Unit.from_json(provider_id, unit_id, entry["unit"])
    emissions = [fact_from_json(fact) for fact in entry["facts"]]
    async with _store_lock_for_loop():
        store.ingest(provider_id, emissions, unit=unit)
        _mark_confirmed(bucket, unit)
        _state.dirty_since_persist = True
        await _maybe_persist(ws_context)


def _mark_confirmed(bucket: BucketKey, unit: Unit) -> None:
    """A refresh updates the bucket's own verdict in place.

    Mutated, not replaced. ``_state.report`` stays keyed to the revision it was
    computed at -- a refresh must not advance that revision -- so an
    ``InterpreterBackend`` built earlier in this same ``run_query`` call
    already holds a reference to this exact ``VerifyReport`` -- swapping in a
    new object here would leave that reference, and the reservations it is
    about to compute from it, pointed at one that still says ``STALE``.
    ``verdicts`` is a plain ``dict`` field on a frozen dataclass: the
    dataclass is immutable, the dict it names is not, and that is precisely
    the seam this needs.
    """
    report = _state.report
    if report is None:
        return
    report.verdicts[bucket] = BucketVerdict(
        kind=Verdict.CONFIRMED,
        unit=unit,
        detail="Re-extracted on demand after its inputs changed.",
    )


async def _retract(ws_context: context.WorkspaceContext, bucket: BucketKey) -> None:
    """The provider was asked about *bucket* and enumerated nothing for it.

    Drops exactly this unit's own facts (``FactStore.retract``) -- never
    cascading, per §4.7 Q8: an edge elsewhere that named a ref this bucket
    used to supply is left dangling rather than followed and deleted. Also
    drops any lingering verdict for the bucket, so a caller does not see a
    reservation for a unit the store no longer holds at all.
    """
    store = _state.store
    if store is None:
        return
    async with _store_lock_for_loop():
        if store.retract(*bucket) == 0:
            return
        if _state.report is not None:
            _state.report.verdicts.pop(bucket, None)
        _state.dirty_since_persist = True
        await _maybe_persist(ws_context)


async def _persist_now(ws_context: context.WorkspaceContext) -> None:
    """Write the store to disk, off the event loop, and re-stamp it.

    **Off the loop because the WM has only one.** ``write_facts`` serializes
    the whole store and has no incremental form: measured at ~1.1s for a 30MB
    fact file, and a fact file grows with the workspace. Run inline that is
    1.1s in which the WM answers nothing at all -- not an LSP request, not an
    MCP call, and not the callback an Extension Runner makes *during the very
    extraction this write is recording*, which has its own timeout and does
    not care that the WM is busy on its behalf.

    Re-stamping ``_state.facts_stamp`` from a ``stat`` taken after the write
    is what stops the WM's own write from looking, to the next ``VERIFIED``
    read, like a foreign out-of-band one -- ``_reload_if_facts_moved`` would
    otherwise drop and reread the store it just wrote.
    """
    store = _state.store
    if store is None:
        return
    path = facts_file_path(ws_context)
    await asyncio.to_thread(write_facts, store, path)
    _state.facts_stamp = _facts_stamp(path)
    _state.dirty_since_persist = False
    _state.last_persisted_at = time.monotonic()


async def _maybe_persist(ws_context: context.WorkspaceContext) -> None:
    """Persist if dirty and the throttle window has elapsed (the second trap).

    See ``_PERSIST_INTERVAL_SEC`` for why this is throttled rather than
    written on every refresh: the file is tens of megabytes and
    ``write_facts`` has no incremental form, so writing it per bucket would
    cost O(whole store) per refresh -- the very cost refreshing one bucket at
    a time exists to avoid.
    """
    if not _state.dirty_since_persist:
        return
    if time.monotonic() - _state.last_persisted_at < _PERSIST_INTERVAL_SEC:
        return
    await _persist_now(ws_context)


async def persist_pending(ws_context: context.WorkspaceContext) -> bool:
    """Flush any refreshed or retracted facts not yet written, unconditionally.

    The throttle in ``_maybe_persist`` bounds what a crash between writes can
    lose to one interval's worth of refreshes -- cheap to re-derive (R17) --
    but a *graceful* shutdown has no reason to accept even that: called from
    ``shutdown_service.on_shutdown``, so a live WM's last few refreshes are
    not left for the next cold start to redo.

    Returns whether anything was actually written. A workspace with no root
    directory (nothing to resolve the fact path against) or no loaded store
    reports ``False`` rather than raising -- shutdown must not fail over a
    write that was never going to have anywhere to land.
    """
    if not _state.dirty_since_persist:
        return False
    try:
        async with _store_lock_for_loop():
            await _persist_now(ws_context)
    except InternalError:
        return False
    return True


class _BridgeHandlers:
    """``knowledge_bridge``'s slot, filled by the module that owns the store.

    A thin object rather than the module itself, so the protocol the runner
    depends on is written down in one place instead of being "whatever this
    module happens to export".
    """

    async def register_schema(self, snapshot: dict) -> bool:
        return await register_schema(snapshot)

    async def run_query(
        self,
        ws_context: context.WorkspaceContext,
        query: dict,
        *,
        mode: str,
        limit: int | None,
    ) -> dict:
        return await run_query(ws_context, query, mode=mode, limit=limit)

    async def fetch_records(
        self, ws_context: context.WorkspaceContext, refs: list[dict]
    ) -> dict:
        return await fetch_records(ws_context, refs)


knowledge_bridge.install(_BridgeHandlers())
