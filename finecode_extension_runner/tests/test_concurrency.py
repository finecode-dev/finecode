from __future__ import annotations

import pytest

from finecode_extension_runner import concurrency


class _FakeAffinity:
    """Callable stand-in for `os.sched_getaffinity(0)` returning a fixed set."""

    def __init__(self, count: int) -> None:
        self._count = count

    def __call__(self, pid: int) -> set[int]:
        return set(range(self._count))


def test_machine_subprocess_budget_uses_affinity_mask_minus_one(monkeypatch) -> None:
    """On Linux, the budget is `len(os.sched_getaffinity(0)) - 1` (headroom for
    the WM's own event loop) — not `os.cpu_count()`, which would report the
    host's full core count regardless of container CPU pinning/quotas.
    """
    monkeypatch.setattr("os.sched_getaffinity", _FakeAffinity(8), raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 999)

    assert concurrency.machine_subprocess_budget() == 7


def test_machine_subprocess_budget_reserves_at_least_one_core(monkeypatch) -> None:
    """Even on a single-core affinity mask, the budget never drops to 0 — a
    0-sized semaphore would deadlock forever.
    """
    monkeypatch.setattr("os.sched_getaffinity", _FakeAffinity(1), raising=False)

    assert concurrency.machine_subprocess_budget() == 1


def test_machine_subprocess_budget_falls_back_to_cpu_count_without_affinity(
    monkeypatch,
) -> None:
    """Non-Linux platforms don't have `os.sched_getaffinity` — fall back to
    `os.cpu_count()` (minus the same one-core headroom).
    """
    monkeypatch.delattr("os.sched_getaffinity", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 5)

    assert concurrency.machine_subprocess_budget() == 4


def test_machine_subprocess_budget_falls_back_to_four_when_cpu_count_is_none(
    monkeypatch,
) -> None:
    monkeypatch.delattr("os.sched_getaffinity", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: None)

    assert concurrency.machine_subprocess_budget() == 3


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (1, 1),
        (3, 2),
        (7, 3),
        (15, 4),
    ],
)
def test_default_layered_concurrency_splits_budget_via_square_root(
    monkeypatch, budget: int, expected: int
) -> None:
    monkeypatch.setattr(concurrency, "machine_subprocess_budget", lambda: budget)

    assert concurrency.default_layered_concurrency() == expected
