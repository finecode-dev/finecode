"""Cap on how many projects a single `run` fan-out drives concurrently.

`prepare-envs` bounds its project fan-out (ADR-0055 layer 1); `run` did not,
even though the same multiplicative composition applies to it: N projects each
dispatching to an ER that spawns up to `ICommandRunner.max_concurrent_processes`
subprocesses (ADR-0056) is N x M concurrent subprocesses. A workspace-wide
`run run_tests` is the worst case.

Each fan-out call builds its own semaphore rather than sharing a
process-global one. Fan-out is re-entrant — a workspace-scoped action's
handler can call back into the WM to fan out again (that is what
`OrchestrationPolicy.max_recursion_depth` bounds) — and a shared semaphore
would let an outer fan-out hold every permit while waiting on an inner one
that can never acquire any. A per-call semaphore cannot deadlock that way; the
cost is that nested fan-outs compose, which the recursion-depth cap bounds.
"""

from __future__ import annotations

import os

from finecode_extension_runner.concurrency import (
    ConcurrencyDecision,
    default_layered_concurrency,
    machine_subprocess_budget,
)

__all__ = ["resolve_run_project_concurrency"]


def resolve_run_project_concurrency() -> ConcurrencyDecision:
    """Effective cap on concurrent projects for one `run` fan-out, with the
    reason it was picked (for logging — see `ConcurrencyDecision`).

    Priority: `FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS` env var (if set) >
    `default_layered_concurrency()`. Like the `prepare-envs` cap this is
    machine-bound rather than project-bound, so it has no
    `finecode-workspace.toml` equivalent — a number tuned for one developer's
    machine would be wrong on everyone else's.

    The default is the sqrt-split, not the full machine budget, for the same
    reason as ADR-0055: this layer composes multiplicatively with the
    per-ER subprocess cap below it.
    """
    if (
        env_value := os.environ.get("FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS")
    ) is not None:
        return ConcurrencyDecision(
            max(int(env_value), 1),
            "FINECODE_WM_RUN_MAX_CONCURRENT_PROJECTS env var",
        )
    return ConcurrencyDecision(
        default_layered_concurrency(),
        f"computed default (machine budget {machine_subprocess_budget()}, sqrt-split)",
    )
