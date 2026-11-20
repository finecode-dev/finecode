"""Attachment point for synchronous-phase timing (for runtime implementations).

Extension authors use :mod:`finecode_extension_api.phase_timing`; this module
is for the ER that installs the timing runtime. The binding is a plain global
so installing it constructs nothing and costs nothing when unused.
"""

from __future__ import annotations

import collections.abc
from typing import Literal, Protocol

__all__ = [
    "CountsKind",
    "IPhaseObservation",
    "IPhaseTimingRuntime",
    "PhaseCounts",
    "PhaseOutcome",
    "PhaseTimingBinding",
    "get_phase_timing_binding",
    "set_phase_timing_binding",
]

PhaseCounts = collections.abc.Mapping[str, int]
"""Per-phase counters: names match ``^[a-z][a-z0-9_]{0,63}$``, values are >= 0."""

PhaseOutcome = Literal["returned", "raised", "cancelled"]
"""How the synchronous body finished."""

CountsKind = Literal["input", "completion"]
"""Which end of the phase the counts describe."""


class IPhaseObservation(Protocol):
    """One timed synchronous phase, owned by the runtime."""

    def set_counts(self, kind: CountsKind, counts: PhaseCounts | None) -> None:
        """Record counts; ``None`` means unavailable, never zero."""
        ...

    def start(self) -> None:
        """Read the start clocks. The runtime calls this around the body."""
        ...

    def stop(self) -> None:
        """Read the stop clocks and save the work-stop wall time."""
        ...

    def close(self, outcome: PhaseOutcome) -> None:
        """Set the outcome attributes and end the span."""
        ...

    def abort(self) -> None:
        """Best-effort rollback after partial entry."""
        ...


class IPhaseTimingRuntime(Protocol):
    """Factory for per-phase observations, owned by the runtime."""

    def open(self, label: str) -> IPhaseObservation:
        """Create an observation for one phase; ends with ``close``/``abort``."""
        ...


class PhaseTimingBinding(Protocol):
    """The runtime's attachment handle installed into the API."""

    def resolve(self) -> IPhaseTimingRuntime:
        """Return the runtime, constructing it if needed."""
        ...

    def report_contained_failure(self) -> None:
        """Count one contained API-side failure (S10 under traces)."""
        ...


_binding: PhaseTimingBinding | None = None


def set_phase_timing_binding(binding: PhaseTimingBinding | None) -> None:
    """Install or remove the runtime binding. Constructs nothing."""
    global _binding
    _binding = binding


def get_phase_timing_binding() -> PhaseTimingBinding | None:
    """Return the installed binding, if any."""
    return _binding
