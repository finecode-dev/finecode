"""The observability guide must name the exact signals an operator needs.

The health summary's log group, its INFO-by-default level, and the diagnostic
profile's settings are load-bearing strings: dashboards, alerts and support
playbooks quote them. If the guide stops naming them, the tests below fail
before an operator discovers the gap.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = (
    Path(__file__).resolve().parent.parent.parent
    / "docs"
    / "guides"
    / "observability.md"
)


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_guide_names_health_summary_group() -> None:
    """The health summary's log group is the handle operators filter on."""
    assert "finecode_extension_runner.shared_telemetry.health" in _guide()


def test_guide_states_info_when_no_loss() -> None:
    """INFO-by-default is the promise that makes a missing WARNING meaningful."""
    text = _guide()
    assert "INFO" in text
    assert "no evidence lost" in text or "no loss" in text


def test_guide_states_diagnostic_profile_settings() -> None:
    """The profile's endpoint, sampler, level and pipelines must be written down."""
    text = _guide()
    assert "OTEL_TRACES_SAMPLER" in text
    assert "parentbased_always_on" in text
    assert "traces" in text and "metrics" in text and "logs" in text
    assert "INFO" in text
