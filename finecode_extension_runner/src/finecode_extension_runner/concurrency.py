"""Shared machine-sizing primitives for bounding concurrent subprocess fan-out.

Both the WM (`src/finecode`) and the ER (`finecode_extension_runner`) size
their concurrency caps from this module — it is the one place both processes
can compute identical, independently-executed defaults without duplicating the
formula. See ADR-0055 (bound `prepare-envs` concurrency at two layers) and
ADR-0063 (bound ER startup concurrency) for the full rationale.

Only the shared primitives live here. Each cap's *resolver* — which decides
how CLI flags, env vars and service config override the default — lives next
to the code that owns that cap, since each has a different override chain.
"""
from __future__ import annotations

import dataclasses
import math
import os


@dataclasses.dataclass(frozen=True)
class ConcurrencyDecision:
    """A resolved concurrency cap plus a short, human-readable reason it was
    picked. Callers log both, so a user can tell from the log alone whether
    a surprising value came from an explicit override or the computed
    default — without reading source (see ADR-0055)."""

    value: int
    source: str


def machine_subprocess_budget() -> int:
    """How many CPU/IO-heavy subprocesses (e.g. `uv install`) this machine
    can run concurrently without starving other processes of scheduling
    time — notably the WM's own event loop.

    Uses the CPU affinity mask (os.sched_getaffinity), not os.cpu_count():
    affinity respects container CPU pinning/quotas (cpuset, --cpus), which
    matters because FineCode commonly runs inside devcontainers where
    cpu_count() reports the host's full core count regardless of the
    container's actual allotment. Falls back to os.cpu_count() on
    non-Linux platforms, where sched_getaffinity is unavailable.

    Reserves one core so the WM (and the rest of the OS) keeps a
    guaranteed scheduling slot even when every subprocess budget slot is
    in use — this headroom is what the bug above was missing.
    """
    try:
        available = len(os.sched_getaffinity(0))
    except AttributeError:
        available = os.cpu_count() or 4
    return max(available - 1, 1)


def default_layered_concurrency() -> int:
    """Default cap for EACH of the two independent concurrency layers
    (projects-in-parallel during prepare-envs, and envs-in-parallel /
    subprocesses-in-parallel within one Extension Runner).

    The two layers compose multiplicatively in the worst case (N projects
    each running M envs concurrently = N*M concurrent subprocesses), so
    handing each layer the full machine_subprocess_budget() independently
    would let the product overshoot the machine's real budget by up to a
    squared factor (e.g. 7 -> 49 on an 8-core box). Splitting the shared
    budget via its square root keeps the worst-case product close to the
    single-machine budget instead of squaring it, while still giving each
    layer genuine parallelism:

        budget=1  -> 1  (product 1)
        budget=3  -> 2  (product 4,  vs target 3)
        budget=7  -> 3  (product 9,  vs target 7)
        budget=15 -> 4  (product 16, vs target 15)

    Each layer is independently configurable; this is only the value used
    when a layer's own setting is left unset.
    """
    return max(round(math.sqrt(machine_subprocess_budget())), 1)


__all__ = [
    "ConcurrencyDecision",
    "machine_subprocess_budget",
    "default_layered_concurrency",
]
