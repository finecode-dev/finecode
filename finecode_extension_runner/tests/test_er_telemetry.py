from __future__ import annotations

from pathlib import Path

import pytest

from finecode_extension_runner import er_telemetry

# PRD-0004-AC7: each telemetry-provider initializer is a no-op when its
# resolved endpoint is falsy, and the ER only initializes once per process.


@pytest.fixture(autouse=True)
def _reset_telemetry_initialized():
    """OTel forbids replacing a TracerProvider/MeterProvider once set, so
    apply_telemetry_config only runs its init_* calls once per process
    lifetime — reset that guard between tests."""
    prev = er_telemetry._telemetry_initialized
    yield
    er_telemetry._telemetry_initialized = prev


def _patch_inits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        er_telemetry, "init_otel_logging", lambda **k: calls.append("logging")
    )
    monkeypatch.setattr(
        er_telemetry, "init_tracer_provider", lambda **k: calls.append("tracer")
    )
    monkeypatch.setattr(
        er_telemetry, "init_meter_provider", lambda **k: calls.append("meter")
    )
    return calls


def test_apply_telemetry_config_noop_when_endpoint_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    er_telemetry._telemetry_initialized = False
    calls = _patch_inits(monkeypatch)

    er_telemetry.apply_telemetry_config({}, project_path=Path("/tmp/proj"))

    assert calls == []
    assert er_telemetry._telemetry_initialized is False


def test_apply_telemetry_config_noop_when_endpoint_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string otlp_endpoint in config must not arm telemetry.

    Mirrors the WM-side precedence rule (PRD-0004-AC6): a present-but-empty
    value means "off", not "connect to nothing".
    """
    er_telemetry._telemetry_initialized = False
    calls = _patch_inits(monkeypatch)

    er_telemetry.apply_telemetry_config(
        {"otlp_endpoint": ""}, project_path=Path("/tmp/proj")
    )

    assert calls == []
    assert er_telemetry._telemetry_initialized is False


def test_apply_telemetry_config_initializes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call with telemetry already initialized is a no-op.

    OTel's SDK raises if a TracerProvider/MeterProvider is replaced after
    being set once — this guard is what makes re-applying ER config safe.
    """
    er_telemetry._telemetry_initialized = False
    calls = _patch_inits(monkeypatch)

    er_telemetry.apply_telemetry_config(
        {"otlp_endpoint": "http://otel-lgtm:4317"}, project_path=Path("/tmp/proj")
    )
    er_telemetry.apply_telemetry_config(
        {"otlp_endpoint": "http://otel-lgtm:4317"}, project_path=Path("/tmp/proj")
    )

    assert calls == ["logging", "tracer", "meter"]
    assert er_telemetry._telemetry_initialized is True
