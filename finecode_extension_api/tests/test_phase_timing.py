"""A fault inside phase timing must never change the work being timed.

Each diagnostic boundary -- binding lookup, runtime construction, observation
opening, count recording, start, stop, close and the count providers -- can
raise, and the body's result, exception or cancellation must come out
unchanged while the failure is counted once. Without this, adding timing
around a config render could break the render.
"""

from __future__ import annotations

import asyncio
import collections.abc
from typing import Any

import pytest

from finecode_extension_api import phase_timing, phase_timing_runtime


class FakeObservation:
    """Recording observation with per-method failure injection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail: set[str] = set()

    def _run(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise RuntimeError(f"{name} boom")

    def set_counts(self, kind: object, counts: object) -> None:
        self._run("set_counts", kind, counts)

    def start(self) -> None:
        self._run("start")

    def stop(self) -> None:
        self._run("stop")

    def close(self, outcome: object) -> Any:
        self._run("close", outcome)
        return True

    def abort(self) -> None:
        self._run("abort")


class FakeRuntime:
    def __init__(self, observation: FakeObservation) -> None:
        self.observation = observation
        self.open_calls = 0
        self.fail_open = False

    def open(self, label: str) -> FakeObservation:
        self.open_calls += 1
        if self.fail_open:
            raise RuntimeError("open boom")
        assert label == "scope"
        return self.observation


class FakeBinding:
    def __init__(self, observation: FakeObservation) -> None:
        self.runtime = FakeRuntime(observation)
        self.failures = 0
        self.fail_resolve = False

    def resolve(self) -> FakeRuntime:
        if self.fail_resolve:
            raise RuntimeError("resolve boom")
        return self.runtime

    def report_contained_failure(self) -> None:
        self.failures += 1


@pytest.fixture()
def installed() -> collections.abc.Iterator[tuple[FakeBinding, FakeObservation]]:
    observation = FakeObservation()
    binding = FakeBinding(observation)
    previous = phase_timing_runtime.get_phase_timing_binding()
    phase_timing_runtime.set_phase_timing_binding(binding)
    try:
        yield binding, observation
    finally:
        phase_timing_runtime.set_phase_timing_binding(previous)


def _names(observation: FakeObservation) -> list[str]:
    return [name for name, _ in observation.calls]


def test_body_result_unchanged_and_counts_recorded(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """The happy path records input counts, completion counts and the outcome."""
    binding, observation = installed
    scope = phase_timing.synchronous_phase("scope", input_counts=lambda: {"a": 1})
    with scope:
        scope.completion_counts(lambda: {"b": 2})
        value = 40 + 2

    assert value == 42
    assert _names(observation) == [
        "set_counts",
        "start",
        "stop",
        "set_counts",
        "close",
    ]
    assert observation.calls[0] == ("set_counts", ("input", {"a": 1}))
    assert observation.calls[3] == ("set_counts", ("completion", {"b": 2}))
    assert observation.calls[4] == ("close", ("returned",))
    assert binding.failures == 0


def test_body_exception_identity_preserved_with_raised_outcome(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """The body's exception propagates unchanged; the outcome says raised."""
    binding, observation = installed
    marker = ValueError("original")
    with pytest.raises(ValueError) as exc_info, phase_timing.synchronous_phase("scope"):
        raise marker
    assert exc_info.value is marker
    assert observation.calls[-1] == ("close", ("raised",))
    assert binding.failures == 0


def test_cancellation_maps_to_cancelled_and_propagates(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """Cancellation is data for the outcome, never something to suppress."""
    binding, observation = installed
    with pytest.raises(asyncio.CancelledError), phase_timing.synchronous_phase("scope"):
        raise asyncio.CancelledError()
    assert observation.calls[-1] == ("close", ("cancelled",))
    assert binding.failures == 0


def test_truthy_close_result_ignored(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """The fake's ``close`` returns ``True``; the scope exit stays ``False``."""
    _binding, observation = installed
    with (
        pytest.raises(RuntimeError, match="original"),
        phase_timing.synchronous_phase("scope"),
    ):
        raise RuntimeError("original")
    assert observation.calls[-1] == ("close", ("raised",))


def test_no_binding_runs_only_the_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a binding no count provider runs and no runtime is touched."""
    monkeypatch.setattr(phase_timing, "get_phase_timing_binding", lambda: None)
    input_calls: list[None] = []
    completion_calls: list[None] = []
    scope = phase_timing.synchronous_phase(
        "scope", input_counts=lambda: input_calls.append(None) or {"a": 1}
    )
    with scope:
        scope.completion_counts(lambda: completion_calls.append(None) or {"b": 2})
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert input_calls == []
    assert completion_calls == []


def test_lookup_failure_stays_inactive_without_counting_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising lookup cannot count S10 -- the binding that would carry the
    count is exactly what failed to load -- so it degrades to inactive."""

    def _raise() -> None:
        raise RuntimeError("lookup boom")

    monkeypatch.setattr(phase_timing, "get_phase_timing_binding", _raise)
    with phase_timing.synchronous_phase("scope", input_counts=lambda: {"a": 1}):
        outcome = "body-ran"
    assert outcome == "body-ran"


def test_resolve_failure_leaves_body_unchanged(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """Runtime construction failing counts once and never opens a scope."""
    binding, observation = installed
    binding.fail_resolve = True
    with phase_timing.synchronous_phase("scope") as scope:
        scope.completion_counts(lambda: {"b": 2})
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert binding.failures == 1
    assert observation.calls == []


def test_open_failure_needs_no_abort(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """Opening failing means nothing was entered, so there is nothing to roll back."""
    binding, observation = installed
    binding.runtime.fail_open = True
    with phase_timing.synchronous_phase("scope"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert binding.failures == 1
    assert observation.calls == []


def test_input_set_counts_failure_aborts_entry(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """A failed input-count record aborts the entry: no start, no exit calls."""
    binding, observation = installed
    observation.fail.add("set_counts")
    with phase_timing.synchronous_phase("scope"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert _names(observation) == ["set_counts", "abort"]
    assert binding.failures == 1


def test_start_failure_aborts_entry(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """A failed start aborts the entry: no stop or close follows."""
    binding, observation = installed
    observation.fail.add("start")
    with phase_timing.synchronous_phase("scope"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert _names(observation) == ["set_counts", "start", "abort"]
    assert binding.failures == 1


def test_stop_failure_still_closes(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """A failed stop still records completion and closes with the outcome."""
    binding, observation = installed
    observation.fail.add("stop")
    with phase_timing.synchronous_phase("scope"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert _names(observation) == [
        "set_counts",
        "start",
        "stop",
        "set_counts",
        "close",
    ]
    assert observation.calls[-1] == ("close", ("returned",))
    assert binding.failures == 1


def test_close_failure_does_not_break_body_result(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """A failed close counts once; the body's value and exceptions are unchanged."""
    binding, observation = installed
    observation.fail.add("close")
    with phase_timing.synchronous_phase("scope"):
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert binding.failures == 1
    marker = RuntimeError("original")
    with (
        pytest.raises(RuntimeError) as exc_info,
        phase_timing.synchronous_phase("scope"),
    ):
        raise marker
    assert exc_info.value is marker
    assert binding.failures == 2


def test_count_provider_failures_mean_unavailable(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """Raising count providers record unavailable counts; the scope continues."""
    binding, observation = installed

    def _raise_counts() -> dict[str, int]:
        raise RuntimeError("counts boom")

    scope = phase_timing.synchronous_phase("scope", input_counts=_raise_counts)
    with scope:
        scope.completion_counts(_raise_counts)
        outcome = "body-ran"
    assert outcome == "body-ran"
    assert observation.calls[0] == ("set_counts", ("input", None))
    assert observation.calls[3] == ("set_counts", ("completion", None))
    assert observation.calls[-1] == ("close", ("returned",))
    assert binding.failures == 2


def test_completion_provider_not_called_after_aborted_entry(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """After an aborted entry the exit runs nothing, not even the provider."""
    _binding, observation = installed
    observation.fail.add("start")
    provider_calls: list[None] = []
    scope = phase_timing.synchronous_phase("scope")
    with scope:
        scope.completion_counts(lambda: provider_calls.append(None) or {"b": 2})
    assert provider_calls == []
    assert _names(observation) == ["set_counts", "start", "abort"]


def test_nested_scopes_measure_inclusively(
    installed: tuple[FakeBinding, FakeObservation],
) -> None:
    """Nested scopes each open their own observation on the same runtime."""
    binding, observation = installed
    outer = phase_timing.synchronous_phase("scope")
    with outer:
        inner = phase_timing.synchronous_phase("scope")
        with inner:
            pass
    assert binding.runtime.open_calls == 2
    assert _names(observation).count("start") == 2
    assert _names(observation).count("close") == 2
