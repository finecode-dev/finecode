"""ER telemetry setup happens once, inside `initialize`.

The WM sends its telemetry settings as `initializationOptions.telemetry`;
the ER makes one provider setup attempt and reports per-signal status back
in the `initialize` result. A second `initialize` replays the first result
without attempting anything again.
"""

from __future__ import annotations

import collections.abc
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest

from finecode_extension_runner import er_server, er_telemetry, global_state
from finecode_extension_runner.shared_telemetry import providers as shared_providers


@pytest.fixture(autouse=True)
def _reset_telemetry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Iterator[list[bool]]:
    """Keep the process-global telemetry guards hermetic between tests.

    Activation (monitor, binding, summary) is faked and recorded here; the
    real paths are covered by the startup tests. Yields the recorded
    `metrics_active` flags.
    """
    monkeypatch.setattr(er_telemetry, "_telemetry_initialized", False)
    monkeypatch.setattr(global_state, "env_name", "test")
    monkeypatch.setattr(global_state, "project_dir_path", Path("/tmp/proj"))
    activations: list[bool] = []

    def _record_activation(_server: typing.Any, *, metrics_active: bool) -> None:
        activations.append(metrics_active)

    monkeypatch.setattr(er_server, "_activate_er_diagnostics", _record_activation)
    yield activations


def _server_stub() -> typing.Any:
    return SimpleNamespace(_telemetry_setup_result=None)


def _initialization_options(
    telemetry: dict[str, typing.Any] | None,
) -> dict[str, typing.Any]:
    params: dict[str, typing.Any] = {"capabilities": {}}
    if telemetry is not None:
        params["initializationOptions"] = {"telemetry": telemetry}
    return params


async def _send_initialize(
    server: typing.Any, params: dict[str, typing.Any]
) -> dict[str, typing.Any]:
    """Drive the real `_on_initialize` handler in this process."""
    return await er_server._on_initialize(server, params)


async def test_initialize_without_telemetry_option_leaves_signals_disabled(
    monkeypatch: pytest.MonkeyPatch, _reset_telemetry_state: list[bool]
) -> None:
    """No telemetry options means disabled signals and no setup attempt."""
    calls: list[dict[str, typing.Any]] = []

    def _record_setup(**kwargs: typing.Any) -> typing.Any:
        calls.append(kwargs)
        raise AssertionError("setup must not run for a disabled value")

    monkeypatch.setattr(shared_providers, "setup_providers", _record_setup)
    server = _server_stub()

    result = await _send_initialize(server, _initialization_options(None))

    assert result["telemetry"] == {
        "traces": "disabled",
        "metrics": "disabled",
        "logs": "disabled",
    }
    assert calls == []
    for signal in ("traces", "metrics", "logs"):
        assert er_telemetry._health.snapshot()["signals"][signal]["availability"] == (
            "disabled"
        )
    second = await _send_initialize(server, _initialization_options(None))
    assert second == result
    assert calls == []
    assert _reset_telemetry_state == []


async def test_initialize_with_otlp_option_sets_up_once(
    monkeypatch: pytest.MonkeyPatch, _reset_telemetry_state: list[bool]
) -> None:
    """A non-disabled value sets up once; a repeat returns the stored result."""
    from finecode_extension_runner.shared_telemetry.providers import (
        ProviderSetupResult,
    )

    calls: list[dict[str, typing.Any]] = []

    def _fake_setup(**kwargs: typing.Any) -> ProviderSetupResult:
        calls.append(kwargs)
        er_telemetry._health.set_availability("traces", "active")
        er_telemetry._health.set_availability("metrics", "active")
        er_telemetry._health.set_availability("logs", "unavailable", "provider_error")
        return ProviderSetupResult(
            traces="active",
            metrics="active",
            logs="unavailable",
            reasons={"logs": "provider_error"},
        )

    monkeypatch.setattr(shared_providers, "setup_providers", _fake_setup)
    server = _server_stub()

    result = await _send_initialize(
        server,
        _initialization_options(
            {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
        ),
    )

    assert result["telemetry"] == {
        "traces": "active",
        "metrics": "active",
        "logs": "unavailable",
    }
    assert len(calls) == 1
    assert calls[0]["service_name"] == "finecode.er.test"
    assert calls[0]["resource_attrs"]["project.path"] == "/tmp/proj"
    assert calls[0]["endpoint"] == "http://otel-lgtm:4317"
    assert er_telemetry._telemetry_initialized is True
    assert er_telemetry._health.snapshot()["signals"]["traces"]["availability"] == (
        "active"
    )
    assert er_telemetry._health.snapshot()["signals"]["logs"]["availability"] == (
        "unavailable"
    )
    assert er_telemetry._health.snapshot()["signals"]["logs"].get(
        "unavailable_reason"
    ) == ("provider_error")
    second = await _send_initialize(
        server,
        _initialization_options(
            {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
        ),
    )
    assert second == result
    assert len(calls) == 1
    assert _reset_telemetry_state == [True]


async def test_initialize_with_unparsable_endpoint_marks_signals_unavailable(
    _reset_telemetry_state: list[bool],
) -> None:
    """An unparsable endpoint leaves every signal unavailable with a reason."""
    server = _server_stub()

    result = await _send_initialize(
        server,
        _initialization_options({"mode": "otlp", "otlpEndpoint": "http://localhost"}),
    )

    assert result["telemetry"] == {
        "traces": "unavailable",
        "metrics": "unavailable",
        "logs": "unavailable",
    }
    for signal in ("traces", "metrics", "logs"):
        health = er_telemetry._health.snapshot()["signals"][signal]
        assert health["availability"] == "unavailable"
        assert health.get("unavailable_reason") == "invalid_setting"
    assert _reset_telemetry_state == [False]
