"""Time one synchronous phase of extension work.

A phase is a lexical scope that runs synchronously on one thread: no
``await``, no suspension, no thread hop inside. The runtime records one span
per phase with elapsed and thread-CPU time separately, so a slow phase can be
told apart from waiting on something else.

Thread CPU covers only the calling thread: worker threads and subprocesses
are unattributed elapsed time, and nested scopes each measure their own span
(the inner span's time is included in the outer span's elapsed time).
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
from typing import Any, Literal, Self

from finecode_extension_api.phase_timing_runtime import (
    IPhaseObservation,
    PhaseCounts,
    PhaseOutcome,
    PhaseTimingBinding,
    get_phase_timing_binding,
)

__all__ = [
    "PhaseScope",
    "synchronous_phase",
]


def _report(binding: PhaseTimingBinding) -> None:
    """Count one contained failure through the binding, never raising."""
    with contextlib.suppress(Exception):
        binding.report_contained_failure()


_FAILED: Any = object()
"""Sentinel for a guarded call that raised (distinct from a ``None`` result)."""


def _call(
    binding: PhaseTimingBinding, func: collections.abc.Callable[..., Any], *args: Any
) -> Any:
    """Run a runtime/provider call; on ``Exception`` count S10 and yield ``_FAILED``.

    ``BaseException`` (cancellation included) is never contained: suppressing
    it would change what the caller observes.
    """
    try:
        return func(*args)
    except Exception:  # noqa: BLE001 - contained: the body must be unaffected
        _report(binding)
        return _FAILED


def _as_counts(value: Any) -> PhaseCounts | None:
    """Provider results are mappings; anything else means unavailable."""
    if isinstance(value, collections.abc.Mapping):
        return value
    return None


class PhaseScope:
    """One synchronous phase. Enter, run the body inline, exit.

    The body is not wrapped in a callable and never retried: ``__enter__``
    arms the observation, the body runs on the caller's frame, and
    ``__exit__`` always returns ``False`` so the body's result or exception is
    unchanged.
    """

    def __init__(
        self,
        label: str,
        input_counts: collections.abc.Callable[[], PhaseCounts] | None = None,
    ) -> None:
        self._label = label
        self._input_provider = input_counts
        self._completion_provider: collections.abc.Callable[[], PhaseCounts] | None = (
            None
        )
        self._binding: PhaseTimingBinding | None = None
        self._observation: IPhaseObservation | None = None
        self._entered = False

    def completion_counts(
        self, provider: collections.abc.Callable[[], PhaseCounts]
    ) -> None:
        """Register the provider read in ``__exit__`` for completion counts."""
        self._completion_provider = provider

    def __enter__(self) -> Self:
        try:
            binding = get_phase_timing_binding()
        except Exception:  # noqa: BLE001 - no binding reachable, stay inactive
            return self
        if binding is None:
            return self
        self._binding = binding
        runtime = _call(binding, binding.resolve)
        if runtime is None or runtime is _FAILED:
            if runtime is None:
                # A binding that delivers no runtime violates its protocol.
                _report(binding)
            return self
        counts: PhaseCounts | None = None
        if self._input_provider is not None:
            counts = _as_counts(_call(binding, self._input_provider))
        observation = _call(binding, runtime.open, self._label)
        if observation is None or observation is _FAILED:
            if observation is None:
                _report(binding)
            return self
        self._observation = observation
        if _call(binding, observation.set_counts, "input", counts) is _FAILED:
            return self._abort_entry()
        if _call(binding, observation.start) is _FAILED:
            return self._abort_entry()
        self._entered = True
        return self

    def _abort_entry(self) -> Self:
        """Roll back a partially entered observation, then stay inactive."""
        observation, self._observation = self._observation, None
        binding, self._binding = self._binding, None
        if observation is not None and binding is not None:
            _call(binding, observation.abort)
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> Literal[False]:
        observation, self._observation = self._observation, None
        binding, self._binding = self._binding, None
        entered, self._entered = self._entered, False
        if not entered or observation is None or binding is None:
            return False
        if exc_type is None:
            outcome: PhaseOutcome = "returned"
        elif isinstance(exc_type, type) and issubclass(
            exc_type, asyncio.CancelledError
        ):
            outcome = "cancelled"
        else:
            outcome = "raised"
        _call(binding, observation.stop)
        completion: PhaseCounts | None = None
        if self._completion_provider is not None:
            completion = _as_counts(_call(binding, self._completion_provider))
        _call(binding, observation.set_counts, "completion", completion)
        _call(binding, observation.close, outcome)
        return False


def synchronous_phase(
    label: str,
    *,
    input_counts: collections.abc.Callable[[], PhaseCounts] | None = None,
) -> PhaseScope:
    """Open a timed synchronous phase called ``label``.

    Use as a plain lexical scope around synchronous work that runs to
    completion on this thread::

        with synchronous_phase("render", input_counts=lambda: {...}):
            render(...)

    Without an installed runtime binding only the binding lookup runs: no
    count provider is called and no clocks are read.
    """
    return PhaseScope(label, input_counts=input_counts)
