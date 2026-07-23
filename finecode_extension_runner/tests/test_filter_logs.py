from __future__ import annotations

from types import SimpleNamespace

import pytest

from finecode_extension_runner import logs


def _record(name: str, level_no: int) -> dict:
    return {"name": name, "level": SimpleNamespace(no=level_no)}


@pytest.fixture(autouse=True)
def _reset_log_level_state():
    """Restore module-level filter state after each test.

    filter_logs is shared, unmodified, by every sink (file, stdout, and the
    OTel export sink in telemetry.py/er_telemetry.py) — PRD-0004-AC4 requires
    it to gate all of them identically, so tests here exercise the function
    directly rather than a specific sink.
    """
    prev_default = logs._default_log_level
    prev_groups = dict(logs.log_level_by_group)
    yield
    logs._default_log_level = prev_default
    logs.log_level_by_group.clear()
    logs.log_level_by_group.update(prev_groups)


def test_filter_logs_default_threshold_blocks_below_default() -> None:
    logs.set_default_log_level(logs.LogLevel.INFO)
    assert logs.filter_logs(_record("finecode.wm_server", logs.LogLevel.DEBUG)) is False


def test_filter_logs_default_threshold_allows_at_or_above_default() -> None:
    logs.set_default_log_level(logs.LogLevel.INFO)
    assert logs.filter_logs(_record("finecode.wm_server", logs.LogLevel.INFO)) is True
    assert logs.filter_logs(_record("finecode.wm_server", logs.LogLevel.ERROR)) is True


def test_filter_logs_group_override_uses_its_own_threshold() -> None:
    logs.set_default_log_level(logs.LogLevel.INFO)
    logs.set_log_level_for_group("finecode.noisy", logs.LogLevel.ERROR)

    assert logs.filter_logs(_record("finecode.noisy.sub", logs.LogLevel.WARNING)) is False
    assert logs.filter_logs(_record("finecode.noisy.sub", logs.LogLevel.ERROR)) is True
    # An unrelated module keeps the default threshold, unaffected by the override.
    assert logs.filter_logs(_record("finecode.other", logs.LogLevel.WARNING)) is True


def test_filter_logs_group_override_with_none_level_blocks_everything() -> None:
    """A group explicitly set to level=None is fully silenced, even at CRITICAL.

    Distinct from "no override" (falls through to the default threshold) —
    this is the explicit-disable branch in filter_logs.
    """
    logs.set_default_log_level(logs.LogLevel.INFO)
    logs.set_log_level_for_group("finecode.silenced", None)

    assert logs.filter_logs(_record("finecode.silenced", logs.LogLevel.CRITICAL)) is False


def test_filter_logs_uses_longest_matching_group_prefix() -> None:
    logs.set_default_log_level(logs.LogLevel.INFO)
    logs.set_log_level_for_group("finecode", logs.LogLevel.WARNING)
    logs.set_log_level_for_group("finecode.wal", logs.LogLevel.DEBUG)

    # More specific group ("finecode.wal") wins over the shorter parent match.
    assert logs.filter_logs(_record("finecode.wal.writer", logs.LogLevel.DEBUG)) is True
    # A sibling under the shorter prefix only gets the parent's threshold.
    assert logs.filter_logs(_record("finecode.other", logs.LogLevel.DEBUG)) is False
    assert logs.filter_logs(_record("finecode.other", logs.LogLevel.WARNING)) is True
