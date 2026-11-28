"""Tracker tests: loss counting must be total and silent.

If two threads report export failures at once, the summary must show both —
a lost count hides the very evidence the health summary exists to surface.
Counts saturate instead of overflowing, flags never clear, and the snapshot
carries exactly the documented shape with no nested structures.
"""

from __future__ import annotations

import threading

from finecode_extension_runner.shared_telemetry.health import HealthTracker


def test_concurrent_counts_sum_exactly() -> None:
    """Concurrent loss reports from exporter threads must not lose counts."""
    tracker = HealthTracker(process_role="er")

    def _report() -> None:
        for _ in range(250):
            tracker.count("traces", "export_failed")

    threads = [threading.Thread(target=_report) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert tracker.snapshot()["signals"]["traces"]["counts"]["export_failed"] == 2000


def test_counts_saturate_at_2_pow_63_minus_1() -> None:
    tracker = HealthTracker(process_role="wm")

    tracker.count("logs", "queue_overflow", n=2**63 - 1)
    tracker.count("logs", "queue_overflow", n=10)

    assert (
        tracker.snapshot()["signals"]["logs"]["counts"]["queue_overflow"] == 2**63 - 1
    )


def test_non_positive_counts_are_ignored() -> None:
    tracker = HealthTracker(process_role="wm")

    tracker.count("metrics", "export_failed", n=0)
    tracker.count("metrics", "export_failed", n=-3)

    assert tracker.snapshot()["signals"]["metrics"]["counts"]["export_failed"] == 0


def test_flags_are_sticky_and_never_clear() -> None:
    tracker = HealthTracker(process_role="er")

    assert tracker.snapshot()["late_admission_flags"] == {
        "contention": False,
        "clock_failure": False,
    }

    tracker.set_flag("contention")
    tracker.set_flag("clock_failure")

    assert tracker.snapshot()["late_admission_flags"] == {
        "contention": True,
        "clock_failure": True,
    }


def test_snapshot_shape_matches_the_documented_records() -> None:
    """The summary is the health contract: flat scalars, fixed keys, no nesting."""
    tracker = HealthTracker(
        process_role="wm",
        process_incarnation="abc123",
        read_log_levels=lambda: [{"group": "g", "level": "INFO"}],
    )
    tracker.set_availability("traces", "unavailable", reason="provider_error")
    tracker.count("traces", "export_failed")
    tracker.note_er_setup("partly_unavailable")

    summary = tracker.snapshot()

    assert summary["process_role"] == "wm"
    assert summary["process_incarnation"] == "abc123"
    assert isinstance(summary["pid"], int)
    assert isinstance(summary["reported_at"], str)
    assert isinstance(summary["sampler"], str)
    assert summary["log_levels"] == [{"group": "g", "level": "INFO"}]
    assert set(summary["signals"].keys()) == {"traces", "metrics", "logs"}
    traces = summary["signals"]["traces"]
    assert traces["availability"] == "unavailable"
    assert traces.get("unavailable_reason") == "provider_error"
    assert traces["counts"] == {
        "sampled_out": 0,
        "queue_overflow": 0,
        "export_failed": 1,
        "suppressed": 0,
        "contained_failure": 0,
    }
    assert summary["late_admission_flags"] == {
        "contention": False,
        "clock_failure": False,
    }
    er_counts = summary.get("er_setup_counts")
    assert er_counts is not None
    assert er_counts == {
        "all_active": 0,
        "partly_unavailable": 1,
        "all_unavailable": 0,
        "disabled": 0,
    }


def test_er_snapshot_carries_lag_monitor_and_no_er_counts() -> None:
    tracker = HealthTracker(process_role="er")
    tracker.set_availability("lag_monitor", "active")

    summary = tracker.snapshot()

    assert set(summary["signals"].keys()) == {
        "traces",
        "metrics",
        "logs",
        "lag_monitor",
    }
    lag_monitor = summary["signals"].get("lag_monitor")
    assert lag_monitor is not None
    assert lag_monitor["availability"] == "active"
    assert summary.get("er_setup_counts") is None


def test_change_callback_fires_but_never_breaks_counting() -> None:
    seen: list[str] = []

    def _on_change() -> None:
        seen.append("changed")
        raise RuntimeError("a failing emission hook must not break counting")

    tracker = HealthTracker(process_role="wm", on_change=_on_change)
    tracker.count("traces", "sampled_out")
    tracker.set_availability("traces", "active")
    tracker.set_flag("contention")
    tracker.note_er_setup("disabled")
    tracker.release_er_setup("disabled")

    assert len(seen) == 5
    assert tracker.snapshot()["signals"]["traces"]["counts"]["sampled_out"] == 1
    assert tracker.snapshot().get("er_setup_counts", {}).get("disabled") == 0
