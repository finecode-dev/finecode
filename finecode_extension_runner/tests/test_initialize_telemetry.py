"""Startup activation: setup failures, repetition, and the monitor/binding
lifecycle behind one `initialize` exchange.

A provider that fails, a monitor that cannot arm, or a binding that cannot
install must never fail startup: the ER reports what it could not do and
keeps serving. Only the first `initialize` in a process may attempt anything.
"""

from __future__ import annotations

import collections.abc
import time
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest
from finecode_extension_api import phase_timing_runtime
from loguru import logger

from finecode_extension_runner import er_server, er_telemetry, global_state
from finecode_extension_runner.shared_telemetry import providers as shared_providers
from finecode_extension_runner.shared_telemetry.health import HealthTracker


@pytest.fixture()
def _hermetic(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Iterator[HealthTracker]:
    """Isolate every process-global the activation path touches."""
    health = HealthTracker(process_role="er")
    monkeypatch.setattr(er_telemetry, "_health", health)
    monkeypatch.setattr(er_telemetry, "_telemetry_initialized", False)
    monkeypatch.setattr(er_telemetry, "_emitter", None)
    monkeypatch.setattr(global_state, "env_name", "test")
    monkeypatch.setattr(global_state, "project_dir_path", Path("/tmp/proj"))
    monkeypatch.setattr(shared_providers, "_attempted", set())
    monkeypatch.setattr(shared_providers, "_statuses", {})
    monkeypatch.setattr(shared_providers, "_reasons", {})
    yield health
    monkeypatch.undo()
    phase_timing_runtime.set_phase_timing_binding(None)


def _server_stub() -> typing.Any:
    return SimpleNamespace(_telemetry_setup_result=None)


def monitor_active(server: typing.Any) -> bool:
    monitor = getattr(server, "_lag_monitor", None)
    return monitor is not None and monitor.availability == "active"


def _options(telemetry: dict[str, typing.Any] | None) -> dict[str, typing.Any]:
    params: dict[str, typing.Any] = {"capabilities": {}}
    if telemetry is not None:
        params["initializationOptions"] = {"telemetry": telemetry}
    return params


async def _initialize(
    server: typing.Any, telemetry: dict[str, typing.Any] | None
) -> dict[str, typing.Any]:
    return await er_server._on_initialize(server, _options(telemetry))


async def _stop_activation(server: typing.Any) -> None:
    """Release everything a real activation installed."""
    monitor = getattr(server, "_lag_monitor", None)
    server._lag_monitor = None
    if monitor is not None:
        await monitor.stop()
    phase_timing_runtime.set_phase_timing_binding(None)
    emitter = er_telemetry._emitter
    if emitter is not None:
        emitter.detach()
        er_telemetry._emitter = None


def _no_global_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real setup without installing process-global providers."""
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace

    def _no_provider(provider: object) -> None:
        return None

    def _no_sink(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(otel_trace, "set_tracer_provider", _no_provider)
    monkeypatch.setattr(otel_metrics, "set_meter_provider", _no_provider)
    monkeypatch.setattr(logger, "add", _no_sink)


async def test_each_provider_failing_in_turn_leaves_others_active(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One signal's setup failure stays contained: the rest report active,
    `initialize` succeeds, and nothing is retried."""
    for failing, setup_name in (
        ("traces", "_setup_traces"),
        ("metrics", "_setup_metrics"),
        ("logs", "_setup_logs"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(shared_providers, "_attempted", set())
            patch.setattr(shared_providers, "_statuses", {})
            patch.setattr(shared_providers, "_reasons", {})

            def _raiser(
                name: str,
            ) -> collections.abc.Callable[..., typing.NoReturn]:
                def _raise(**kwargs: typing.Any) -> typing.NoReturn:
                    raise RuntimeError(f"{name} boom")

                return _raise

            patch.setattr(shared_providers, setup_name, _raiser(setup_name))
            _no_global_install(patch)
            server = _server_stub()

            result = await _initialize(
                server, {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
            )

            for signal in ("traces", "metrics", "logs"):
                if signal == failing:
                    assert result["telemetry"][signal] == "unavailable"
                else:
                    assert result["telemetry"][signal] == "active"
            assert er_telemetry._telemetry_initialized is True
            await _stop_activation(server)


async def test_unparsable_endpoint_signals_unavailable_but_monitor_active(
    _hermetic: HealthTracker,
) -> None:
    """An unparsable endpoint fails the signals, never the monitor or setup."""
    health = _hermetic
    server = _server_stub()
    try:
        result = await _initialize(
            server, {"mode": "otlp", "otlpEndpoint": "http://localhost"}
        )

        assert result["telemetry"] == {
            "traces": "unavailable",
            "metrics": "unavailable",
            "logs": "unavailable",
        }
        for signal in ("traces", "metrics", "logs"):
            assert health.snapshot()["signals"][signal]["availability"] == "unavailable"
        monitor = server._lag_monitor
        assert monitor is not None
        assert monitor.availability == "active"
        assert phase_timing_runtime.get_phase_timing_binding() is not None
    finally:
        await _stop_activation(server)


async def test_sdk_disabled_affects_result_only(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited `OTEL_SDK_DISABLED` fails the signals; the monitor still
    activates and the response still answers."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    server = _server_stub()
    try:
        result = await _initialize(
            server, {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
        )

        assert result["telemetry"] == {
            "traces": "unavailable",
            "metrics": "unavailable",
            "logs": "unavailable",
        }
        assert monitor_active(server)
    finally:
        await _stop_activation(server)


async def test_blocking_setup_delays_response_without_added_deadline(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup runs inside the existing `initialize` exchange: a 2 s block
    delays the response by about 2 s, and no other timer fires."""

    def _slow_setup(**kwargs: typing.Any) -> typing.Any:
        time.sleep(2.0)
        return SimpleNamespace(traces="active", metrics="active", logs="active")

    monkeypatch.setattr(shared_providers, "setup_providers", _slow_setup)
    activations: list[bool] = []

    def _record_activation(server: typing.Any, *, metrics_active: bool) -> None:
        activations.append(metrics_active)

    monkeypatch.setattr(er_server, "_activate_er_diagnostics", _record_activation)
    server = _server_stub()

    started = time.monotonic()
    result = await _initialize(
        server, {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
    )
    elapsed = time.monotonic() - started

    assert result["telemetry"] == {
        "traces": "active",
        "metrics": "active",
        "logs": "active",
    }
    assert 1.9 <= elapsed < 10.0
    assert activations == [True]


async def test_raising_monitor_arm_with_providers_leaves_it_unavailable(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With providers up, a failing arm still answers and marks unavailable."""

    def _fake_setup(**kwargs: typing.Any) -> typing.Any:
        return SimpleNamespace(traces="active", metrics="active", logs="active")

    monkeypatch.setattr(shared_providers, "setup_providers", _fake_setup)

    class _BoomMonitor:
        def arm(self) -> typing.NoReturn:
            raise RuntimeError("arm boom")

        def start(self) -> None:
            raise AssertionError("start must not run after a failed arm")

    monkeypatch.setattr(er_server.er_lag_monitor, "ErLagMonitor", _BoomMonitor)
    health = _hermetic
    server = _server_stub()
    try:
        result = await _initialize(
            server, {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
        )

        assert result["telemetry"]["traces"] == "active"
        assert getattr(server, "_lag_monitor", None) is None
        assert health.snapshot()["signals"]["lag_monitor"]["availability"] == (
            "unavailable"
        )
        assert (
            health.snapshot()["signals"]["lag_monitor"].get("unavailable_reason")
            == "monitor_start_failed"
        )
    finally:
        await _stop_activation(server)


async def test_raising_binding_setter_clears_binding_and_counts_s10(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setter that changes then raises leaves the binding absent and counts
    one contained failure; the response still answers."""

    def _fake_setup(**kwargs: typing.Any) -> typing.Any:
        return SimpleNamespace(traces="active", metrics="active", logs="active")

    monkeypatch.setattr(shared_providers, "setup_providers", _fake_setup)
    real_set = phase_timing_runtime.set_phase_timing_binding
    calls: list[typing.Any] = []

    def _change_then_raise(binding: typing.Any) -> None:
        calls.append(binding)
        if len(calls) == 1:
            real_set(object())
            raise RuntimeError("setter boom")
        real_set(binding)

    monkeypatch.setattr(er_server, "set_phase_timing_binding", _change_then_raise)
    health = _hermetic
    server = _server_stub()
    try:
        result = await _initialize(
            server, {"mode": "otlp", "otlpEndpoint": "http://otel-lgtm:4317"}
        )

        assert result["telemetry"]["traces"] == "active"
        assert phase_timing_runtime.get_phase_timing_binding() is None
        assert len(calls) == 2
        assert (
            health.snapshot()["signals"]["traces"]["counts"]["contained_failure"] == 1
        )
    finally:
        await _stop_activation(server)


async def test_update_config_does_not_touch_telemetry(
    _hermetic: HealthTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config push never sets up providers or activates diagnostics."""
    from types import SimpleNamespace as _NS

    setup_calls: list[dict] = []
    activations: list[bool] = []

    def _record_setup(**kwargs: typing.Any) -> typing.NoReturn:
        setup_calls.append(kwargs)
        raise AssertionError("setup must not run at updateConfig time")

    monkeypatch.setattr(shared_providers, "setup_providers", _record_setup)

    def _record_activation(server: typing.Any, *, metrics_active: bool) -> None:
        activations.append(metrics_active)

    monkeypatch.setattr(er_server, "_activate_er_diagnostics", _record_activation)

    async def _empty_changes():
        if False:
            yield None

    class _EmptyChangeStream:
        async def __aenter__(self):
            return _empty_changes()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeSession:
        def subscribe_to_changes_of_opened_files(self) -> _EmptyChangeStream:
            return _EmptyChangeStream()

    class _FakeSessionCM:
        async def __aenter__(self) -> _FakeSession:
            return _FakeSession()

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeEditor:
        def session(self, author: object) -> _FakeSessionCM:
            return _FakeSessionCM()

    class _FakeRegistry:
        async def get_instance(self, iface: object) -> _FakeEditor:
            return _FakeEditor()

        async def dispose_all(self) -> None:
            return None

    async def _fake_update_config(**kwargs: object) -> tuple[object, object]:
        response = _NS(to_dict=lambda: {"ok": True})
        runner_context = _NS(
            action_cache_by_name={}, di_registry=_FakeRegistry(), wal_writer=None
        )
        return response, runner_context

    monkeypatch.setattr(er_server.services, "update_config", _fake_update_config)
    server = er_server.ErServer()
    params = {
        "workingDir": "/tmp",
        "projectName": "p",
        "projectDefPath": "/tmp/pyproject.toml",
        "config": {"actions": [], "action_handler_configs": {}},
    }
    await er_server.update_config(server, params)
    await er_server.update_config(server, params)

    assert setup_calls == []
    assert activations == []
