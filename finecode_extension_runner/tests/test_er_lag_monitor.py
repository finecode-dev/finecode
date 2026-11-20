"""The ER reports its own event-loop starvation instead of leaving an operator
to infer it from a WM-side RPC timeout.

When the loop is starved, the ER records the lag together with the process
and host state that says what the machine was doing. The records must stay
bounded (one warning per stall per cooldown, one recovery line when it ends)
and silent on a healthy loop, and the monitor must never outlive its server,
or the signal that makes the next starvation report attributable becomes
noise nobody reads -- or a task nobody owns.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import os
import pathlib
import time
import typing
from types import SimpleNamespace

import pytest
from finecode_common.lag import LagEpisodeTracker, LagSettings, ProcessUsage
from loguru import logger

from finecode_extension_runner import er_lag_monitor, er_server, global_state
from finecode_extension_runner.shared_telemetry.health import (
    HealthTracker,
    SignalHealth,
)

_WARNING_MESSAGE = "ER event loop lag exceeded threshold"
_RECOVERY_MESSAGE = "ER event loop lag recovered"
_INTERRUPTED_MESSAGE = "ER event loop lag episode interrupted"


@contextlib.contextmanager
def _capture(level: str = "INFO"):
    records: list[dict[str, typing.Any]] = []
    sink_id = logger.add(lambda message: records.append(message.record), level=level)
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _lag_records(
    records: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    return [r for r in records if r["message"].startswith("ER event loop lag")]


def _lag_signal(health: HealthTracker) -> SignalHealth:
    """The ER summary always carries the monitor signal; narrow the optional key."""
    signals = health.snapshot()["signals"]
    assert "lag_monitor" in signals
    return signals["lag_monitor"]


def _fresh_monitor() -> tuple[er_lag_monitor.ErLagMonitor, HealthTracker]:
    health = HealthTracker(process_role="er")
    return er_lag_monitor.ErLagMonitor(health=health), health


@pytest.fixture()
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the identity extras so the assertions read fixed values."""
    monkeypatch.setattr(global_state, "env_name", "test-env")
    monkeypatch.setattr(global_state, "project_dir_path", pathlib.Path("/tmp/proj"))


async def _block_after_settling(monitor: er_lag_monitor.ErLagMonitor) -> None:
    """Let one on-time sample fire, so the block below is unambiguously what
    made a later sample late rather than the task's own first scheduling."""
    monitor.arm()
    monitor.start()
    await asyncio.sleep(0.6)


# ---------------------------------------------------------------------------
# P6.3 Lag evidence
# ---------------------------------------------------------------------------


async def test_synthetic_block_warns_then_recovers_with_context(
    _identity: None,
) -> None:
    """A block past threshold plus interval must leave one warning and one
    recovery carrying the process and host state.

    Without them, ER starvation is only visible as a timeout on the WM side,
    with no evidence in the ER's own log of when it started or what the
    machine was doing.
    """
    monitor, _health = _fresh_monitor()
    with _capture(level="INFO") as records:
        try:
            await _block_after_settling(monitor)
            time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
            await asyncio.sleep(1.2)
        finally:
            await monitor.stop()

    lag_records = _lag_records(records)
    warnings = [r for r in lag_records if r["message"].startswith(_WARNING_MESSAGE)]
    recoveries = [r for r in lag_records if r["message"].startswith(_RECOVERY_MESSAGE)]
    assert len(warnings) == 1, "one stall must cost exactly one warning"
    assert len(recoveries) == 1, "the stall end must be summarized once"
    extra = warnings[0]["extra"]
    assert 1000 <= extra["lag_ms"] <= 10000
    assert extra["threshold_ms"] == 1000
    assert extra["pid"] > 0
    assert extra["project_path"] == "/tmp/proj"
    assert extra["env_name"] == "test-env"
    for field in (
        "window_ms",
        "process_cpu_ms",
        "process_cpu_pct",
        "voluntary_switches",
        "involuntary_switches",
        "load_1m",
        "cpu_count",
        "ready_callbacks",
        "mem_available_mb",
        "swap_used_mb",
        "psi_memory_full_avg10",
    ):
        assert field in extra, field
    message = warnings[0]["message"]
    assert f"lag={extra['lag_ms']}ms" in message
    assert "(process-wide, all threads)" in message


async def test_block_just_after_a_sample_leaves_no_burst() -> None:
    """A stall must cost one late sample, not a burst of immediate ones.

    The sampler re-arms from the observed wake time, so the missed schedule
    does not turn into catch-up samples that each look like fresh stalls.
    """
    monitor, _health = _fresh_monitor()
    with _capture(level="INFO") as records:
        try:
            monitor.arm()
            monitor.start()
            await asyncio.sleep(0.55)
            time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
            await asyncio.sleep(1.5)
        finally:
            await monitor.stop()

    lag_records = _lag_records(records)
    warnings = [r for r in lag_records if r["message"].startswith(_WARNING_MESSAGE)]
    assert len(warnings) == 1, "re-arming from wake time must not burst"


def test_sustained_overload_warns_per_cooldown_then_recovers() -> None:
    """A continuous stall must cost one warning per cooldown, and the samples
    hidden in between must be counted when the stall ends.

    Unbounded warnings turn a starvation episode into a log flood; a recovery
    line without the suppressed count hides how long the stall lasted.
    """
    settings = LagSettings(
        interval_sec=er_lag_monitor.INTERVAL_SEC,
        threshold_sec=er_lag_monitor.THRESHOLD_SEC,
        cooldown_sec=er_lag_monitor.COOLDOWN_SEC,
        window_sec=er_lag_monitor.WINDOW_SEC,
    )
    tracker = LagEpisodeTracker(settings)
    usage = ProcessUsage(cpu_sec=1.0, voluntary_switches=0, involuntary_switches=0)
    kinds: list[str] = []
    now = 0.0
    while now <= 65.0:
        kinds.append(tracker.observe(now=now, lag=2.0, usage=usage).kind)
        now += 0.5
    event = tracker.observe(now=66.0, lag=0.0, usage=usage)

    assert kinds.count("warning") == 3, "one warning per 30 s over 65 s"
    assert event.kind == "recovered"
    assert event.episode is not None
    assert event.episode.suppressed_warnings == kinds.count("suppressed")
    assert event.episode.max_lag_ms == 2000
    assert kinds.count("suppressed") == len(kinds) - 3


def test_retained_samples_never_exceed_window_bound() -> None:
    """Retention is bounded by the D9 window no matter how long the process
    runs, so the monitor cannot grow memory without bound."""
    settings = LagSettings(
        interval_sec=er_lag_monitor.INTERVAL_SEC,
        threshold_sec=er_lag_monitor.THRESHOLD_SEC,
        cooldown_sec=er_lag_monitor.COOLDOWN_SEC,
        window_sec=er_lag_monitor.WINDOW_SEC,
    )
    tracker = LagEpisodeTracker(settings)
    usage = ProcessUsage(cpu_sec=1.0, voluntary_switches=0, involuntary_switches=0)
    for index in range(3000):
        _ = tracker.observe(now=float(index) * 0.5, lag=0.0, usage=usage)
    assert len(tracker._samples) <= 1201


async def test_healthy_loop_writes_no_lag_record() -> None:
    """On-time samples are routine, not evidence; a healthy ER stays silent."""
    monitor, _health = _fresh_monitor()
    with _capture(level="INFO") as records:
        try:
            monitor.arm()
            monitor.start()
            await asyncio.sleep(1.2)
        finally:
            await monitor.stop()

    assert _lag_records(records) == []


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
async def test_local_file_record_survives_log_provider_failure(
    tmp_path: pathlib.Path, level: str, _identity: None
) -> None:
    """When the log pipeline fails, the local file must still hold the lag
    evidence; a lost warning is a lost stall, whatever else broke."""
    monitor, _health = _fresh_monitor()
    log_file = tmp_path / "er.log"

    def _failing_sink(_message: object) -> None:
        raise RuntimeError("log provider boom")

    failing_id = logger.add(_failing_sink, level="TRACE")
    file_id = logger.add(str(log_file), level=level)
    try:
        try:
            await _block_after_settling(monitor)
            time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
            await asyncio.sleep(1.2)
        finally:
            await monitor.stop()
    finally:
        logger.remove(failing_id)
        logger.remove(file_id)

    text = log_file.read_text(encoding="utf-8")
    assert _WARNING_MESSAGE in text
    assert ("recovered" in text) == (level == "INFO")


# ---------------------------------------------------------------------------
# P6.4 Lifecycle
# ---------------------------------------------------------------------------


async def test_logging_control_during_open_episode_keeps_task_and_episode(
    _identity: None,
) -> None:
    """Runtime logging control must not disturb the monitor: the same task
    keeps sampling and the open episode continues across the change."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    with _capture(level="INFO") as records:
        try:
            await _block_after_settling(monitor)
            time.sleep(1.6)  # noqa: ASYNC251 - open the episode under test
            task = monitor._task
            assert task is not None
            await er_server.update_logging(
                server, {"forward": False, "forwardLevel": "INFO"}
            )
            await er_server.update_logging(
                server, {"forward": False, "forwardLevel": "INFO"}
            )
            assert monitor._task is task
            assert not task.done()
            assert not monitor.terminated.done()
            await asyncio.sleep(1.0)
        finally:
            await monitor.stop()

    lag_records = _lag_records(records)
    assert (
        len([r for r in lag_records if r["message"].startswith(_WARNING_MESSAGE)]) == 1
    )
    assert (
        len([r for r in lag_records if r["message"].startswith(_RECOVERY_MESSAGE)]) == 1
    )


async def test_retire_runner_context_keeps_monitor() -> None:
    """Retiring the outgoing RunnerContext releases DI services, not the
    server-owned monitor."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    try:
        monitor.arm()
        monitor.start()
        task = monitor._task
        assert task is not None
        await er_server._retire_runner_context(server, None)
        assert monitor._task is task
        assert not task.done()
    finally:
        await monitor.stop()


async def test_update_config_twice_during_open_episode_keeps_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config push to a running ER rebuilds the RunnerContext around the
    monitor without disturbing its task or its open episode."""

    class _NoChanges(collections.abc.AsyncIterator[None]):
        async def __anext__(self) -> None:
            raise StopAsyncIteration

    class _EmptyChangeStream:
        async def __aenter__(self) -> _NoChanges:
            return _NoChanges()

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
        response = SimpleNamespace(to_dict=lambda: {"ok": True})
        runner_context = SimpleNamespace(
            action_cache_by_name={},
            di_registry=_FakeRegistry(),
            wal_writer=None,
        )
        return response, runner_context

    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    with _capture(level="INFO") as records:
        try:
            await _block_after_settling(monitor)
            time.sleep(1.6)  # noqa: ASYNC251 - open the episode under test
            task = monitor._task
            assert task is not None
            empty_actions: list[object] = []
            empty_configs: dict[str, object] = {}
            params = {
                "workingDir": "/tmp",
                "projectName": "p",
                "projectDefPath": "/tmp/pyproject.toml",
                "config": {
                    "actions": empty_actions,
                    "action_handler_configs": empty_configs,
                },
            }
            monkeypatch.setattr(
                er_server.services, "update_config", _fake_update_config
            )
            await er_server.update_config(server, params)
            await er_server.update_config(server, params)
            assert monitor._task is task
            assert not task.done()
            await asyncio.sleep(1.0)
        finally:
            await monitor.stop()

    lag_records = _lag_records(records)
    assert (
        len([r for r in lag_records if r["message"].startswith(_WARNING_MESSAGE)]) == 1
    )
    assert (
        len([r for r in lag_records if r["message"].startswith(_RECOVERY_MESSAGE)]) == 1
    )


async def test_shutdown_releases_monitor_and_resolves_terminated() -> None:
    """Shutdown stops the sampler task and resolves its terminal signal, and a
    second shutdown is a no-op."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    monitor.arm()
    monitor.start()
    task = monitor._task
    assert task is not None
    await er_server._on_shutdown(server, None)
    assert task.done()
    assert monitor.terminated.done()
    assert server._lag_monitor is None
    await er_server._on_shutdown(server, None)


async def test_release_is_idempotent() -> None:
    """Releasing twice stops the task once and resolves the terminal signal."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    monitor.arm()
    monitor.start()
    task = monitor._task
    assert task is not None
    await er_server._release_lag_monitor(server)
    await er_server._release_lag_monitor(server)
    assert task is not None and task.done()
    assert monitor.terminated.done()
    assert server._lag_monitor is None


async def test_stdio_task_cancel_releases_monitor() -> None:
    """When the stdio server task exits by cancellation, the monitor goes
    with it through the same finally that serves a transport exit."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    monitor.arm()
    monitor.start()
    stdin_read, stdin_write = os.pipe()
    stdin_buf = os.fdopen(stdin_read, "rb")
    stdout_buf = open(os.devnull, "wb")  # noqa: ASYNC230, SIM115 - the transport requires a real fd
    server_task = asyncio.create_task(
        server.start_io_async(stdin_buf=stdin_buf, stdout_buf=stdout_buf)
    )
    try:
        await asyncio.sleep(0.5)
        assert not monitor.terminated.done()
        transport = server._session._transport
        assert transport is not None
        await transport.stop()
        await asyncio.wait_for(server_task, timeout=15)
    finally:
        if not server_task.done():
            _ = server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
        os.close(stdin_write)
        stdin_buf.close()
        stdout_buf.close()
    assert server._lag_monitor is None
    assert monitor.terminated.done()


async def test_tcp_server_exit_releases_monitor() -> None:
    """When the TCP server task ends (here: the one-shot connection closes
    the server), the monitor is released through the server's finally."""
    server = er_server.ErServer()
    monitor, _health = _fresh_monitor()
    server._lag_monitor = monitor
    monitor.arm()
    monitor.start()
    server_task = asyncio.create_task(server._run_tcp("127.0.0.1", 0))
    try:
        for _ in range(100):
            if server._tcp_server is not None and server._tcp_server.sockets:
                break
            await asyncio.sleep(0.05)
        assert server._tcp_server is not None and server._tcp_server.sockets
        port = server._tcp_server.sockets[0].getsockname()[1]
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server_task, timeout=15)
    finally:
        if not server_task.done():
            _ = server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task
    assert server._lag_monitor is None
    assert monitor.terminated.done()


async def test_partial_startup_failure_marks_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When arming fails, the monitor reports unavailable with a reason and
    still resolves its terminal signal."""

    def _raise(loop: asyncio.AbstractEventLoop, settings: object) -> typing.NoReturn:
        raise RuntimeError("no loop time")

    monkeypatch.setattr(er_lag_monitor, "arm_lag_baseline", _raise)
    monitor, health = _fresh_monitor()
    with pytest.raises(RuntimeError):
        monitor.start()
    assert monitor.availability == "unavailable"
    assert _lag_signal(health).get("unavailable_reason") == "monitor_start_failed"
    assert monitor.terminated.done()


async def test_cancel_before_first_turn_resolves_terminated() -> None:
    """Cancelling the sampler before it ever runs still resolves the terminal
    signal, so readiness waiters never hang."""
    monitor, _health = _fresh_monitor()
    monitor.arm()
    monitor.start()
    task = monitor._task
    assert task is not None
    _ = task.cancel()
    for _ in range(100):
        if monitor.terminated.done():
            break
        await asyncio.sleep(0.01)
    assert monitor.terminated.done()
    await monitor.stop()
    assert monitor.terminated.done()


async def test_raising_host_reader_leaves_fields_unavailable(
    monkeypatch: pytest.MonkeyPatch, _identity: None
) -> None:
    """A broken host reader degrades its fields to unavailable and counts the
    containment, but the warning still fires."""

    def _raise() -> typing.NoReturn:
        raise RuntimeError("no loadavg")

    monkeypatch.setattr(er_lag_monitor, "read_host_load", _raise)
    monitor, health = _fresh_monitor()
    with _capture(level="INFO") as records:
        try:
            await _block_after_settling(monitor)
            time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
            await asyncio.sleep(1.2)
        finally:
            await monitor.stop()

    warnings = [
        r for r in _lag_records(records) if r["message"].startswith(_WARNING_MESSAGE)
    ]
    assert len(warnings) == 1, "the warning still fires with degraded fields"
    assert warnings[0]["extra"]["load_1m"] is None
    assert warnings[0]["extra"]["cpu_count"] is None
    counts = _lag_signal(health)["counts"]
    assert counts["contained_failure"] >= 1
    assert _lag_signal(health)["availability"] == "active"


async def test_unexpected_sampler_failure_stops_monitor_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sampler that keeps failing stops the monitor, marks it unavailable,
    and never restarts it."""

    async def _boom(*args: object, **kwargs: object) -> typing.NoReturn:
        raise RuntimeError("sampler boom")

    monkeypatch.setattr(er_lag_monitor, "run_lag_sampler", _boom)
    monitor, health = _fresh_monitor()
    with _capture(level="INFO") as records:
        monitor.arm()
        monitor.start()
        assert monitor._task is not None
        await asyncio.wait_for(monitor._task, timeout=10)
        await asyncio.sleep(0.3)

    assert monitor.availability == "unavailable"
    assert _lag_signal(health).get("unavailable_reason") == "monitor_sampler_failed"
    assert monitor.terminated.done()
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1


async def test_cooldown_suppressions_count_s7() -> None:
    """Samples hidden by the cooldown are counted under the monitor, so the
    health summary can report them without one record per loss."""
    monitor, health = _fresh_monitor()
    with _capture(level="WARNING"):
        for index in range(131):
            monitor._on_sample(now=float(index) * 0.5, lag=2.0)
        monitor._on_sample(now=66.0, lag=0.0)

    counts = _lag_signal(health)["counts"]
    assert counts["suppressed"] == 128
