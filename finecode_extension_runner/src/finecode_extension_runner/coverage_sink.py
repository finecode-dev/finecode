"""Per-run sink for coverage that must cross a bridge building a fresh result.

Some coverage cannot ride the ``update()`` join: a bridge handler that
constructs a *new* result object from selected fields of a sub-action's result
(``format_file``'s callers, the pre-commit bridge) performs no ``update()`` at
all, so the miss would die at the boundary. This module is the second framework
choke point: the action-runner impls deposit every sub-result's coverage into
the current run's sink, and the run loop folds the sink into the run's result
through ``merge_coverage``, so the join stays commutative and per-yield
deposits are O(misses).

Why it is ambient rather than a parameter — the ``docs/guides/developing-finecode.md``
"Ambient state" rule's qualifying blocker (b): the alternative is a public API
change that moves responsibility onto extension authors.
``IProjectActionRunner.run_action`` takes ``(action_type, payload, meta,
caller_kwargs)``; threading the sink in would be a new parameter on
*extension-facing* API, and by the no-default rule a parameter replacing
ambient state gets no default — so it would be required, break every existing
dispatch call site, and make every extension author responsible for forwarding
it, with a forgotten forward failing silently. Blocking it, too, is the rule's
blocker (a) from the depositor's side: ``ProjectActionRunnerImpl`` and
``WorkspaceActionRunnerImpl`` are ``register_instance`` singletons
(``di/bootstrap.py``), one per configuration and reused across every run, so a
per-run sink cannot live on them. The strongest precedent is this very family
of objects reaching for ambient per-run state for an identical reason:
``run_action_in_projects`` names its originating run via
``run_context.current_run_id()`` rather than threading ``run_id`` through every
handler signature. ``caller_kwargs`` cannot carry the sink either: coverage
flows callee → caller while ``caller_kwargs`` flows caller → callee, it is
optional with a default, and it is per-action typed.

**Mutate, never rebind** — scoped to handlers and child tasks. A child task
that calls ``_SINK.set(...)`` writes into its own context copy and its deposit
is lost; one that *mutates* the shared sink object is seen by the parent. The
framework's own bind point (``run()``) must rebind — ``set`` at run entry,
``reset`` at exit — because that is what makes the sink per-run rather than
per-process (``run_context.py``'s ``run()`` exists for the same reason).
Edit this docstring if that changes, not the flat sentence.

Bind scope is per run, at run-loop entry, not at the request handler: a
nested run (the local fast path for a sub-action) binds its own sink, so
attribution survives, and its union lands on *its own* result, which the parent
picks up in the ordinary function return at the four dispatch methods — not by
task-context inheritance. Inheritance carries deposits from a run's own
``TaskGroup`` children to that run's sink and nothing more; removing a
``TaskGroup`` would not break cross-action propagation.

Ambient inside a process, explicit on the wire: a sub-run in another ER
returns its coverage *inside the serialized result* (the ``coverage`` field
travels with the value), and the workspace-runner deposit reads it back from
the returned objects on this side. No sink stitching exists between ERs.

The one residual without a carrier: a run that produces no result object at
all (a handler returns ``None``, sends nothing, yields nothing, and called a
sub-action that reported a miss) cannot receive the fold — ``RESULT_TYPE`` is
not generically constructible. The deposits are dropped and the miss is
already preserved wherever the sub-action's own result survives, so this is
documented-and-dropped rather than synthesised.
"""

from __future__ import annotations

import collections.abc
import contextlib
import contextvars

from finecode_extension_api import code_action
from finecode_extension_api.code_action import (
    CoverageStatus,
    ItemCoverage,
    merge_coverage,
)
from finecode_extension_api.resource_uri import ResourceUri

_UNHANDLED_BLOCK_HEADER = "unhandled:"
_UNHANDLED_BLOCK_MAX_ITEMS_PER_REASON = 10


def render_unhandled_block(
    unhandled: collections.abc.Sequence[ItemCoverage],
) -> str:
    """Render the trailing ``unhandled:`` block appended around ``to_text()``.

    Grouped by reason with a count, and the per-item enumeration capped:
    misses are O(unmatched inputs) and travel to the top-level caller, so an
    unbounded block would flood the default run and get tuned out — the same
    death as losing the signal, reached from the other side.
    The bound is a property of this consumer (the ER's text renderers), not
    of the mechanism: the JSON/LSP path reads coverage uncapped.

    Returns ``""`` for an empty input, so callers can append unconditionally.
    """
    if not unhandled:
        return ""
    by_reason: dict[CoverageStatus, list[ItemCoverage]] = {}
    for entry in unhandled:
        by_reason.setdefault(entry.status, []).append(entry)
    lines = [_UNHANDLED_BLOCK_HEADER]
    for status, entries in sorted(
        by_reason.items(), key=lambda kv: (-len(kv[1]), kv[0].value)
    ):
        lines.append(f"  {status.value} ({len(entries)}):")
        rendered_items = sorted(str(entry.item) for entry in entries)
        for item in rendered_items[:_UNHANDLED_BLOCK_MAX_ITEMS_PER_REASON]:
            lines.append(f"    {item}")
        overflow = len(rendered_items) - _UNHANDLED_BLOCK_MAX_ITEMS_PER_REASON
        if overflow > 0:
            lines.append(f"    ... and {overflow} more")
    return "\n".join(lines) + "\n"


_SINK: contextvars.ContextVar[CoverageSink | None] = contextvars.ContextVar(
    "finecode_coverage_sink", default=None
)


class CoverageSink:
    """Mutable per-run accumulator of coverage entries.

    Handlers and child tasks must mutate this object's ``deposit``, never
    rebind the contextvar (:mod:`coverage_sink` docstring).
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: list[ItemCoverage] = []

    def deposit(self, entries: collections.abc.Iterable[ItemCoverage]) -> None:
        """Fold *entries* in through the rank-max join.

        Goes through ``merge_coverage`` — never a plain append — so the join
        dedupes by item and per-yield deposits stay O(misses) rather than
        O(partials x misses).
        """
        self._entries = merge_coverage(self._entries, entries)

    @property
    def entries(self) -> list[ItemCoverage]:
        return self._entries

    def __bool__(self) -> bool:
        return bool(self._entries)


@contextlib.contextmanager
def run() -> collections.abc.Iterator[None]:
    """Bind a fresh per-run sink for this task, reset on exit.

    Framework-only, at run-loop entry. A nested ``run_action`` call binds its
    own sink; the outer token is restored on exit.
    """
    token = _SINK.set(CoverageSink())
    try:
        yield
    finally:
        _SINK.reset(token)


def current_sink() -> CoverageSink | None:
    """The run's sink, or ``None`` outside a run.

    ``None`` is an ordinary answer rather than a defect, exactly like
    ``run_context.current_run_id()``: outside a dispatched run there is no run
    to accumulate for.
    """
    return _SINK.get()


def bind() -> contextvars.Token[CoverageSink | None]:
    """Set a fresh per-run sink for this task, returning the reset token.

    Framework-only, called at run-loop entry; ``unbind`` restores the token.
    A nested ``run_action`` call binds its own sink, so the outer sink is
    picked up again once the nested run restores it.
    """
    return _SINK.set(CoverageSink())


def unbind(token: contextvars.Token[CoverageSink | None]) -> None:
    """Restore the sink as it was before the run's ``bind``."""
    _SINK.reset(token)


def deposit_from(result: code_action.RunActionResult) -> None:
    """Fold ``result.coverage`` into the current run's sink.

    Called at the four dispatch return points. No-op outside a run.
    """
    sink = _SINK.get()
    if sink is None:
        return
    sink.deposit(result.coverage)


def absorb_coverage(items: collections.abc.Iterable[ResourceUri | None]) -> None:
    """Mark inputs as handled by the bridge itself.

    Handlers that genuinely handled the degradation (e.g. a formatter bridge
    that wrote an unformatted dump on purpose) call this mid-run, when the
    run's result object does not exist yet. It deposits an ``ABSORBED`` entry
    per item into the run's sink; the run loop's fold then turns it into
    ordinary coverage data on the run's result, where the rank-max join
    suppresses the original miss wherever the two meet — including across
    nesting and serialization. Not a sink deletion: a recursive ``unhandled``
    read would resurface a miss that only the by-status rank can beat.
    """
    sink = _SINK.get()
    if sink is None:
        return
    sink.deposit(
        ItemCoverage(status=code_action.CoverageStatus.ABSORBED, item=item)
        for item in items
    )


def fold_into(result: code_action.RunActionResult) -> None:
    """Fold the run's sink into ``result.coverage``.

    The run loop calls this before the result leaves the run, at every exit
    point. Goes through ``merge_coverage``, never a list concat, so the sink
    union is the *same* merge rule as the ``update()`` join — otherwise there
    would genuinely be two merge rules and ``unhandled``'s join-on-read would
    become load-bearing rather than defensive.
    """
    sink = _SINK.get()
    if sink is None or not sink.entries:
        return
    result.coverage = merge_coverage(result.coverage, sink.entries)
