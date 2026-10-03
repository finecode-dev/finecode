"""The periodic reporter must observe without ever changing the outcome."""

from __future__ import annotations

import asyncio
import gc
import selectors
import time

import click
import pytest

from finecode.cli_app import resource_usage
from finecode.wm_client import ApiMethodNotFoundError


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0


class _AdvancingSelector(selectors.DefaultSelector):
    """Never blocks: when nothing is ready, jumps the clock to the next timer."""

    def __init__(self, clock: _VirtualClock) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout=None):
        events = super().select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            raise RuntimeError("virtual-time loop would block forever")
        self._clock.now += timeout
        return events


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._virtual = _VirtualClock()
        super().__init__(selector=_AdvancingSelector(self._virtual))

    def time(self) -> float:
        return self._virtual.now

    def stall(self, seconds: float) -> None:
        """Move time forward without running anything, as a blocked loop would."""
        self._virtual.now += seconds


def _run_virtual(poll_latencies, body, interval=4.0, default_latency=0.5):
    """Run periodic() on virtual time; return [(kind, text), ...] in emission order."""
    events: list[tuple[str, str]] = []
    remaining = list(poll_latencies)
    calls = {"count": 0}

    async def _amain():
        loop = asyncio.get_running_loop()

        async def _poll() -> dict:
            calls["count"] += 1
            latency = remaining.pop(0) if remaining else default_latency
            await asyncio.sleep(latency)
            return _snapshot()

        async with resource_usage.periodic(
            _poll,
            interval,
            emit=lambda text: events.append(("line", text)),
            emit_status=lambda text: events.append(("status", text)),
            render=lambda _s, elapsed: f"sample {elapsed}",
            summary=False,
            clock=loop.time,
        ):
            await body(loop)
        return events, calls["count"]

    with asyncio.Runner(loop_factory=_VirtualTimeLoop) as runner:
        return runner.run(_amain())


def _snapshot(**overrides) -> dict:
    base: dict = {
        "timestamp": 100.0,
        "wm": {
            "pid": 1,
            "uptimeSec": 10.0,
            "connectedClients": 1,
            "loopLagMs": 3,
            "loopLagMaxMs": 100,
            "loopLagPendingMs": 0,
            "loopLagWindowSec": 30.0,
        },
        "projects": {"total": 1, "running": 1, "active": 1},
        "runners": {
            "byStatus": {},
            "running": 1,
            "starting": 0,
            "active": 1,
            "byEnv": {},
        },
        "budget": {"total": 7, "source": "test"},
        "workSlots": {
            "total": 4,
            "used": 1,
            "free": 3,
            "waiting": 0,
            "stallEscape": False,
            "holders": [],
        },
        "startupSlots": {"total": 3, "used": 0, "free": 3, "waiting": 0},
        "inFlightRuns": [],
        "peaks": {
            "runnersRunning": 1,
            "runnersStarting": 0,
            "projectsActive": 1,
            "workSlotsUsed": 1,
            "workSlotsWaiting": 0,
            "startupSlotsWaiting": 0,
            "hostSwapUsedMb": None,
            "hostMemAvailableMinMb": None,
            "hookFailed": False,
        },
        "host": {
            "memTotalMb": 32000,
            "memAvailableMb": 4100,
            "swapTotalMb": 20000,
            "swapUsedMb": 12000,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": None,
                "ioFullAvg10": None,
                "cpuSomeAvg10": None,
            },
            "load1m": None,
            "cpuCount": 8,
        },
        "processes": None,
    }
    base.update(overrides)
    return base


def test_prompt_answers_produce_no_false_no_answer_and_one_sample_per_interval() -> (
    None
):
    """Prompt answers must not print no answer and must tick once per interval.

    A false no answer line would train operators to ignore the one signal
    that means the server is actually stalled.
    """

    async def _body(_loop) -> None:
        await asyncio.sleep(13.0)

    events, _count = _run_virtual([], _body)
    assert events == [
        ("line", "sample 4.5"),
        ("line", "sample 8.5"),
        ("line", "sample 12.5"),
    ]


def test_slow_poll_reports_missed_ticks_then_resumes_on_grid() -> None:
    """A slow poll must report each missed tick once, then resume on the grid.

    Without one line per missed tick an operator cannot tell how long the
    server was stalled, and off-grid samples would hide the recovery point.
    """

    async def _body(_loop) -> None:
        await asyncio.sleep(21.0)

    events, count = _run_virtual([10.0], _body)
    assert events == [
        ("status", "[resources] t=+8s no answer for 4.00s"),
        ("status", "[resources] t=+12s no answer for 8.00s"),
        ("line", "sample 14.0"),
        ("line", "sample 16.5"),
        ("line", "sample 20.5"),
    ]
    assert count == 3


def test_late_started_poll_gets_a_full_interval() -> None:
    """A poll started late must still wait a full interval before no answer.

    The client loop can stall on its own work; blaming the server for that
    delay would send operators chasing a stall that never happened.
    """

    async def _body(loop) -> None:
        await asyncio.sleep(3.5)
        loop.stall(5.0)
        await asyncio.sleep(5.5)

    events, _count = _run_virtual([], _body)
    assert events == [
        ("line", "sample 9.0"),
        ("line", "sample 13.0"),
    ]


async def test_answered_ticks_emit_lines_and_leave_stdout_alone(capsys) -> None:
    """Each answered poll must produce one stderr line and no stdout.

    Stdout belongs to the action's own output; a reporter line there would
    corrupt piped results.
    """
    emitted: list[str] = []
    statuses: list[str] = []

    async def _poll() -> dict:
        return _snapshot()

    async with resource_usage.periodic(
        _poll,
        0.02,
        emit=emitted.append,
        emit_status=statuses.append,
    ):
        await asyncio.sleep(0.07)

    assert len(emitted) >= 2
    assert all(line.startswith("[resources]") for line in emitted)
    assert capsys.readouterr().out == ""


async def test_pending_poll_emits_no_answer_and_sends_no_second() -> None:
    """A poll still outstanding at the next tick must not be duplicated.

    A second request while the first is unanswered would pile load on the
    very server that is already too stalled to answer.
    """
    emitted: list[str] = []
    statuses: list[str] = []
    calls = {"count": 0}
    release = asyncio.Event()

    async def _poll() -> dict:
        calls["count"] += 1
        await release.wait()
        return _snapshot()

    try:
        async with resource_usage.periodic(
            _poll,
            0.02,
            emit=emitted.append,
            emit_status=statuses.append,
        ):
            await asyncio.sleep(0.07)
            assert calls["count"] == 1
            assert emitted == []
            assert any("no answer for" in line for line in statuses)
            waited = [
                float(line.split("no answer for")[1].rstrip("s"))
                for line in statuses
                if "no answer for" in line
            ]
            assert waited == sorted(waited)
            assert len(set(waited)) == len(waited)
    finally:
        release.set()


async def test_normal_exit_emits_summary_body_exception_still_propagates() -> None:
    """The peaks summary must follow a normal exit and survive a failure.

    Either way the body's own outcome is what the caller sees.
    """
    statuses: list[str] = []

    async def _poll() -> dict:
        return _snapshot()

    async with resource_usage.periodic(
        _poll, 0.02, emit=lambda _line: None, emit_status=statuses.append
    ):
        pass
    assert any("peaks:" in line for line in statuses)

    async def _body() -> int:
        async with resource_usage.periodic(
            _poll, 0.02, emit=lambda _line: None, emit_status=lambda _line: None
        ):
            return 42

    assert await _body() == 42

    async def _failing_body():
        async with resource_usage.periodic(
            _poll, 0.02, emit=lambda _line: None, emit_status=statuses.append
        ):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _failing_body()
    assert any("peaks:" in line for line in statuses)

    statuses.clear()
    task = asyncio.create_task(_failing_body())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unsupported_stops_with_reason() -> None:
    """An old server without the method must quiet the reporter for good.

    Retrying every interval would spam a log explaining a version skew the
    operator already knows about.
    """
    statuses: list[str] = []
    calls = {"count": 0}

    async def _poll() -> dict:
        calls["count"] += 1
        raise ApiMethodNotFoundError(-32601, "nope")

    async with resource_usage.periodic(
        _poll, 0.02, emit=lambda _line: None, emit_status=statuses.append
    ) as state:
        await asyncio.wait_for(state.stopped.wait(), 1.0)

    assert state.stop_reason == "unsupported"
    assert any("not supported" in line for line in statuses)
    first_count = calls["count"]
    await asyncio.sleep(0.05)
    assert calls["count"] == first_count
    assert not any("peaks:" in line for line in statuses)


async def test_disconnect_stops_only_when_asked() -> None:
    """A lost connection keeps ticking for run, but ends a watch.

    A transient reconnect must not silence a long run's reporter, while a
    watch with no reconnect policy must not print forever.
    """

    async def _poll() -> dict:
        raise ConnectionError("gone")

    statuses: list[str] = []
    async with resource_usage.periodic(
        _poll,
        0.02,
        emit=lambda _line: None,
        emit_status=statuses.append,
        stop_on_disconnect=True,
    ) as state:
        await asyncio.wait_for(state.stopped.wait(), 1.0)
    assert state.stop_reason == "disconnected"
    assert any("no connection" in line for line in statuses)

    statuses.clear()
    async with resource_usage.periodic(
        _poll, 0.02, emit=lambda _line: None, emit_status=statuses.append
    ):
        await asyncio.sleep(0.07)
    assert len([line for line in statuses if "no connection" in line]) >= 2


async def test_paused_drops_every_line_kind() -> None:
    """While a question is on screen, no reporter line may interleave.

    The prompt reader owns the terminal until it resolves; a line printed
    underneath it corrupts the answer.
    """
    emitted: list[str] = []
    statuses: list[str] = []
    paused = asyncio.Event()

    async def _poll() -> dict:
        return _snapshot()

    async with resource_usage.periodic(
        _poll,
        0.02,
        emit=emitted.append,
        emit_status=statuses.append,
        paused=paused,
    ):
        await asyncio.sleep(0.05)
        assert emitted == []
        paused.set()
        await asyncio.sleep(0.05)
        assert len(emitted) >= 1

    paused.clear()
    emitted.clear()
    statuses.clear()
    gate = asyncio.Event()

    async def _hanging_poll() -> dict:
        await gate.wait()
        return _snapshot()

    try:
        async with resource_usage.periodic(
            _hanging_poll,
            0.02,
            emit=emitted.append,
            emit_status=statuses.append,
            paused=paused,
        ):
            await asyncio.sleep(0.05)
            assert all("no answer" not in line for line in statuses)
    finally:
        gate.set()


async def test_reporter_errors_never_change_outcome() -> None:
    """A broken formatter must cost a line, never the command's exit.

    The reporter is a diagnostic; turning a green run red because a label
    was missing would make it the failure it was meant to explain.
    """
    statuses: list[str] = []

    def _bad_render(_snapshot: dict, _elapsed: float) -> str:
        raise KeyError("boom")

    async def _poll() -> dict:
        return _snapshot()

    async def _body() -> int:
        async with resource_usage.periodic(
            _poll,
            0.02,
            emit=lambda _line: None,
            emit_status=statuses.append,
            render=_bad_render,
        ):
            await asyncio.sleep(0.07)
            return 42

    assert await _body() == 42
    assert any("reporter error: KeyError" in line for line in statuses)

    async def _failing_body():
        async with resource_usage.periodic(
            _poll,
            0.02,
            emit=lambda _line: None,
            emit_status=statuses.append,
            render=_bad_render,
        ):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _failing_body()


async def test_dead_loop_task_is_reported_and_wakes_waiter() -> None:
    """A loop task that dies must wake whoever waits on it.

    Otherwise a watch would hang forever with no line explaining why.
    """
    statuses: list[str] = []

    async def _dead_loop(*args, **kwargs):
        raise RuntimeError("loop")

    async def _poll() -> dict:
        return _snapshot()

    original = resource_usage._run_loop
    resource_usage._run_loop = _dead_loop  # type: ignore[assignment]
    try:

        async def _body() -> int:
            async with resource_usage.periodic(
                _poll,
                0.02,
                emit=lambda _line: None,
                emit_status=statuses.append,
            ) as state:
                await asyncio.wait_for(state.stopped.wait(), 1.0)
                assert state.stop_reason == "failed"
                assert isinstance(state.error, RuntimeError)
                return 42

        assert await _body() == 42
    finally:
        resource_usage._run_loop = original
    assert len([line for line in statuses if "reporter error" in line]) == 1


async def test_every_emission_is_contained() -> None:
    """Even the reporter's own status lines must not kill the loop.

    A closed stderr on the second line must not silence the third.
    """
    statuses: list[str] = []
    calls = {"count": 0}
    gate = asyncio.Event()

    async def _poll() -> dict:
        await gate.wait()
        return _snapshot()

    def _flaky_status(line: str) -> None:
        calls["count"] += 1
        if calls["count"] <= 2:
            raise KeyError("stderr boom")
        statuses.append(line)

    try:
        async with resource_usage.periodic(
            _poll, 0.02, emit=lambda _line: None, emit_status=_flaky_status
        ) as state:
            await asyncio.sleep(0.10)
            assert not state.stopped.is_set()
            assert any("no answer" in line for line in statuses)
    finally:
        gate.set()


async def test_broken_pipe_stops_quietly() -> None:
    """A closed stdout must end the loop without a reporter error.

    Retrying against a reader that went away would fail every tick.
    """
    statuses: list[str] = []

    async def _poll() -> dict:
        return _snapshot()

    def _broken_emit(_line: str) -> None:
        raise BrokenPipeError("closed")

    async with resource_usage.periodic(
        _poll,
        0.02,
        emit=_broken_emit,
        emit_status=statuses.append,
    ) as state:
        await asyncio.wait_for(state.stopped.wait(), 1.0)

    assert state.stop_reason == "output_closed"
    assert not any("reporter error" in line for line in statuses)


async def test_outer_cancellation_not_swallowed() -> None:
    """Cancelling during exit must still cancel the outer task.

    Swallowing it would turn an interrupted run into a clean one.
    """
    gate = asyncio.Event()

    async def _poll() -> dict:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise
        return _snapshot()

    async def _body() -> None:
        async with resource_usage.periodic(_poll, 0.02, summary=False):
            await asyncio.sleep(0.05)
            return

    task = asyncio.create_task(_body())
    await asyncio.sleep(0.10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()


async def test_exit_order_loop_stopped_before_summary() -> None:
    """The summary must not race a poll the exit just cancelled."""
    order: list[str] = []
    gate = asyncio.Event()

    async def _poll() -> dict:
        if not order or order[-1] != "summary poll sent":
            pass
        await gate.wait()
        return _snapshot()

    original_loop = resource_usage._run_loop

    async def _recording_loop(state, poll, interval_sec, **kwargs):
        order.append("loop task cancelled")
        try:
            await original_loop(state, poll, interval_sec, **kwargs)
        except asyncio.CancelledError:
            order.append("loop task cancelled-done")
            raise

    resource_usage._run_loop = _recording_loop  # type: ignore[assignment]
    original_peaks = resource_usage.format_peaks
    resource_usage.format_peaks = lambda snapshot: (
        order.append(  # type: ignore[assignment]
            "summary poll sent"
        )
        or "peaks"
    )
    try:

        async def _poll_fast() -> dict:
            order.append("summary poll sent")
            return _snapshot()

        async with resource_usage.periodic(
            _poll_fast, 0.02, emit=lambda _line: None, emit_status=lambda _line: None
        ):
            await asyncio.sleep(0.05)
    finally:
        resource_usage._run_loop = original_loop
        resource_usage.format_peaks = original_peaks
        gate.set()

    assert "loop task cancelled" in order
    assert order.index("loop task cancelled") < order.index("summary poll sent")


async def test_failed_polls_are_retrieved() -> None:
    """A poll that failed must not surface as never-retrieved.

    The traceback would point at the reporter instead of the failure it
    was trying to report.
    """
    loop = asyncio.get_running_loop()
    errors: list = []
    old_handler = loop.get_exception_handler()

    def _handler(_loop, context) -> None:
        errors.append(context)

    loop.set_exception_handler(_handler)
    try:
        task: asyncio.Task = asyncio.create_task(_failing())
        await asyncio.sleep(0)
        await resource_usage._reap_poll(task)
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(old_handler)


async def _failing() -> None:
    raise ValueError("poll boom")


async def test_no_emission_after_exit() -> None:
    """A poll answering after exit must produce no line.

    The context is gone; a late line would land in the next command's output.
    """
    emitted: list[str] = []
    gate = asyncio.Event()

    async def _poll() -> dict:
        await gate.wait()
        return _snapshot()

    async with resource_usage.periodic(
        _poll, 0.02, emit=emitted.append, emit_status=lambda _line: None
    ):
        pass
    gate.set()
    await asyncio.sleep(0.05)
    assert emitted == []


def test_formatters_handle_all_null() -> None:
    """All-null snapshots must format to n/a without raising.

    Non-Linux hosts and containers without cgroup or PSI are normal.
    """
    snapshot = _snapshot(
        wm={
            "pid": None,
            "uptimeSec": None,
            "connectedClients": None,
            "loopLagMs": None,
            "loopLagMaxMs": None,
            "loopLagPendingMs": None,
            "loopLagWindowSec": None,
        },
        host={
            "memTotalMb": None,
            "memAvailableMb": 4100,
            "swapTotalMb": None,
            "swapUsedMb": None,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": None,
                "ioFullAvg10": None,
                "cpuSomeAvg10": None,
            },
            "load1m": None,
            "cpuCount": None,
        },
        peaks={
            "runnersRunning": 0,
            "runnersStarting": 0,
            "projectsActive": 0,
            "workSlotsUsed": 0,
            "workSlotsWaiting": 0,
            "startupSlotsWaiting": 0,
            "hostSwapUsedMb": None,
            "hostMemAvailableMinMb": None,
            "hookFailed": False,
        },
    )

    assert "n/a" in resource_usage.format_line(snapshot, 3.0)
    assert "n/a" in resource_usage.format_peaks(snapshot)


async def test_summary_timeout_emits_unavailable_quickly() -> None:
    """A summary poll that never answers must not delay exit."""
    statuses: list[str] = []

    async def _hang() -> dict:
        await asyncio.Event().wait()
        return _snapshot()

    started = time.monotonic()
    async with resource_usage.periodic(
        _hang,
        0.02,
        emit=lambda _line: None,
        emit_status=statuses.append,
        summary_timeout_sec=0.05,
    ):
        await asyncio.sleep(0.03)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert any("peaks unavailable" in line for line in statuses)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2", 2.0)],
)
def test_parse_interval_valid(value, expected) -> None:
    assert (
        resource_usage.parse_interval(value, source="s", zero_disables=True) == expected
    )


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "601", "abc"])
def test_parse_interval_rejects(value) -> None:
    with pytest.raises(resource_usage.InvalidResourceUsageInterval, match="s"):
        resource_usage.parse_interval(value, source="s", zero_disables=True)


def test_parse_interval_zero_and_window() -> None:
    assert resource_usage.parse_interval("0", source="s", zero_disables=True) is None
    with pytest.raises(resource_usage.InvalidResourceUsageInterval):
        resource_usage.parse_interval("0", source="s", zero_disables=False)
    assert resource_usage.lag_window_for(15) == 30
    assert resource_usage.lag_window_for(120) == 120


def test_emit_stderr_tty_prefix(monkeypatch) -> None:
    """On a TTY the line must clear the progress line first.

    Off a TTY (CI) there is no progress line to clear.
    """

    class _Err:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    written: list[str] = []
    monkeypatch.setattr(click, "echo", lambda msg, err=False: written.append(msg))
    monkeypatch.setattr(click, "get_text_stream", lambda name: _Err(True))
    resource_usage._emit_stderr("hello")
    assert written[-1].startswith("\r\033[K")

    monkeypatch.setattr(click, "get_text_stream", lambda name: _Err(False))
    resource_usage._emit_stderr("hello")
    assert not written[-1].startswith("\r\033[K")


@pytest.mark.parametrize(
    ("environ", "flag_value", "disabled", "expected"),
    [
        ({}, None, False, None),
        ({"CI": "true"}, None, False, 15.0),
        ({"CI": "true", resource_usage.ENV_VAR: "0"}, None, False, None),
        ({"CI": "true", resource_usage.ENV_VAR: "5"}, None, False, 5.0),
        ({}, "2", False, 2.0),
        ({"CI": "true"}, None, True, None),
    ],
)
def test_resolve_interval_precedence(environ, flag_value, disabled, expected) -> None:
    """Flag beats env var beats CI default beats off.

    An operator must be able to reason about which switch won without
    reading the implementation.
    """
    assert (
        resource_usage.resolve_interval(
            flag_value=flag_value, disabled=disabled, environ=environ
        )
        == expected
    )


@pytest.mark.parametrize(
    ("environ", "flag_value", "disabled"),
    [
        ({"CI": "true"}, "2", True),
        ({resource_usage.ENV_VAR: "abc"}, None, False),
        ({resource_usage.ENV_VAR: "nan"}, None, False),
        ({}, "-1", False),
        ({}, "abc", False),
    ],
)
def test_resolve_interval_rejects(environ, flag_value, disabled) -> None:
    with pytest.raises(resource_usage.InvalidResourceUsageInterval):
        resource_usage.resolve_interval(
            flag_value=flag_value, disabled=disabled, environ=environ
        )


def test_resolve_interval_names_sources() -> None:
    """Rejections must name the switch that carried the bad value."""
    with pytest.raises(
        resource_usage.InvalidResourceUsageInterval, match="--resource-usage"
    ):
        resource_usage.resolve_interval(flag_value="abc", disabled=False, environ={})
    with pytest.raises(
        resource_usage.InvalidResourceUsageInterval,
        match=resource_usage.ENV_VAR,
    ):
        resource_usage.resolve_interval(
            flag_value=None,
            disabled=False,
            environ={resource_usage.ENV_VAR: "abc"},
        )


def test_format_line_shows_psi() -> None:
    """The tick line must show PSI beside swap so thrashing is visible live."""
    pressured = _snapshot(
        host={
            "memTotalMb": 17920,
            "memAvailableMb": 545,
            "swapTotalMb": 20728,
            "swapUsedMb": 20727,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": 76.91,
                "ioFullAvg10": 1.25,
                "cpuSomeAvg10": 0.18,
            },
            "load1m": None,
            "cpuCount": 8,
        }
    )
    assert "swap 20.2G/20.2G psi 77% · lag" in resource_usage.format_line(
        pressured, 15.0
    )

    calm = _snapshot(
        host={
            "memTotalMb": 17920,
            "memAvailableMb": 10000,
            "swapTotalMb": 20728,
            "swapUsedMb": 0,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": None,
                "ioFullAvg10": None,
                "cpuSomeAvg10": None,
            },
            "load1m": None,
            "cpuCount": 8,
        }
    )
    assert "psi n/a · lag" in resource_usage.format_line(calm, 15.0)

    old_wm = _snapshot(
        host={
            "memTotalMb": 17920,
            "memAvailableMb": 10000,
            "swapTotalMb": 20728,
            "swapUsedMb": 0,
            "cgroup": None,
            "load1m": None,
            "cpuCount": 8,
        }
    )
    assert "psi n/a" in resource_usage.format_line(old_wm, 15.0)


def test_format_peaks_shows_psi_max_and_footprint() -> None:
    """The final line must carry the episode's worst PSI and its footprint."""
    snapshot = _snapshot(
        peaks={
            "runnersRunning": 328,
            "runnersStarting": 0,
            "projectsActive": 1,
            "workSlotsUsed": 1,
            "workSlotsWaiting": 0,
            "startupSlotsWaiting": 0,
            "hostSwapUsedMb": 20727,
            "hostMemAvailableMinMb": 545,
            "hostPsiMemoryFullMax": 82.24,
            "hookFailed": False,
        },
        processes={
            "wm": {"pid": 1, "processCount": 1, "rssMb": 307, "swapMb": 100},
            "runners": [
                {
                    "runner": f"r{i}",
                    "pid": i,
                    "processCount": 1,
                    "rssMb": 48,
                    "swapMb": 12,
                }
                for i in range(328)
            ],
            "untracked": [
                {"pid": 9000, "processCount": 1, "rssMb": 10, "swapMb": 1},
                {"pid": 9001, "processCount": 1, "rssMb": 11, "swapMb": 1},
            ],
            "totalRssMb": 16179,
            "totalSwapMb": 4198,
        },
    )

    line = resource_usage.format_peaks(snapshot)

    assert "psi max 82%" in line
    assert "footprint 15.8G rss + 4.1G swap (WM 0.3G, 328 runners, 2 untracked)" in line

    without_walk = _snapshot(processes=None)
    assert "footprint n/a" in resource_usage.format_peaks(without_walk)

    busy = _snapshot(processes={"error": "process walk already in progress"})
    assert "footprint n/a (process walk already in progress)" in (
        resource_usage.format_peaks(busy)
    )


def test_format_table_shows_pressure_and_psi_max() -> None:
    """The on-demand table must name the pressure verdict and the PSI peak."""
    snapshot = _snapshot(
        host={
            "memTotalMb": 17920,
            "memAvailableMb": 545,
            "swapTotalMb": 20728,
            "swapUsedMb": 20727,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": 76.91,
                "ioFullAvg10": 1.25,
                "cpuSomeAvg10": 0.18,
            },
            "load1m": None,
            "cpuCount": 8,
            "memoryPressure": {"active": True, "reasons": ["psi", "memoryExhausted"]},
        },
        peaks={
            "runnersRunning": 1,
            "runnersStarting": 0,
            "projectsActive": 1,
            "workSlotsUsed": 1,
            "workSlotsWaiting": 0,
            "startupSlotsWaiting": 0,
            "hostSwapUsedMb": 20727,
            "hostMemAvailableMinMb": 545,
            "hostPsiMemoryFullMax": 82.24,
            "hookFailed": False,
        },
    )

    table = resource_usage.format_table({"version": "test", "clients": []}, snapshot)

    assert "pressure yes (psi, memoryExhausted)" in table
    assert "psi max 82%" in table


def _pressure_snapshot(*, active: bool) -> dict:
    reasons = ["psi", "memoryExhausted"] if active else []
    return _snapshot(
        host={
            "memTotalMb": 17920,
            "memAvailableMb": 545,
            "swapTotalMb": 20728,
            "swapUsedMb": 20727,
            "cgroup": None,
            "psi": {
                "memoryFullAvg10": 76.91,
                "ioFullAvg10": 1.25,
                "cpuSomeAvg10": 0.18,
            },
            "load1m": None,
            "cpuCount": 8,
            "memoryPressure": {"active": active, "reasons": reasons},
        },
        processes={
            "wm": {"pid": 1, "processCount": 1, "rssMb": 307, "swapMb": 100},
            "runners": [],
            "untracked": [],
            "totalRssMb": 16179,
            "totalSwapMb": 4198,
        },
    )


async def test_reporter_warns_once_per_episode() -> None:
    """A pressure episode must warn once and clear only after two calm polls.

    A single calm sample inside an episode is noise, not recovery; warning
    again on every pressured tick would train operators to ignore the line.
    """
    actives = [False, True, True, True, False, True, False, False, True, True]
    elapsed_list = [15, 30, 45, 60, 75, 90, 105, 120, 135, 150]
    snapshots = [_pressure_snapshot(active=active) for active in actives]
    calls: list[bool] = []
    state = {"i": 0}

    async def _fake(*, include_processes=False, lag_window_sec=None):
        calls.append(include_processes)
        snapshot = snapshots[state["i"]]
        state["i"] += 1
        return snapshot

    reporter = resource_usage.RunReporter(_fake, lag_window_sec=30.0)
    rendered: list[tuple[int, str]] = []
    for elapsed in elapsed_list:
        snapshot = await reporter.poll()
        rendered.append((elapsed, reporter.render(snapshot, float(elapsed))))

    warnings = [elapsed for elapsed, text in rendered if "!! memory pressure" in text]
    assert warnings == [30, 135]

    cleared = [
        (elapsed, text)
        for elapsed, text in rendered
        if "memory pressure cleared" in text
    ]
    assert len(cleared) == 1
    assert cleared[0][0] == 120
    assert "began t=+30s" in cleared[0][1]

    footprints = [
        elapsed for elapsed, text in rendered if "memory pressure footprint:" in text
    ]
    assert footprints == [45, 150]

    assert calls == [
        False,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
    ]


async def test_reporter_footprint_survives_failed_poll() -> None:
    """A poll that fails while a footprint is owed must not lose the walk.

    The next poll has to ask again, or the episode's one footprint never
    happens and the final line cannot name the heaviest processes.
    """
    snapshots = [
        _pressure_snapshot(active=False),
        _pressure_snapshot(active=True),
        _pressure_snapshot(active=True),
    ]
    calls: list[bool] = []
    state = {"i": 0, "fail_once": True}

    async def _fake(*, include_processes=False, lag_window_sec=None):
        calls.append(include_processes)
        if state["fail_once"] and len(calls) == 3:
            raise RuntimeError("poll boom")
        snapshot = snapshots[state["i"]]
        state["i"] += 1
        return snapshot

    reporter = resource_usage.RunReporter(_fake, lag_window_sec=30.0)
    first = await reporter.poll()
    reporter.render(first, 15.0)
    second = await reporter.poll()
    reporter.render(second, 30.0)
    with pytest.raises(RuntimeError, match="poll boom"):
        await reporter.poll()
    fourth = await reporter.poll()
    text = reporter.render(fourth, 60.0)

    assert calls == [False, False, True, True]
    assert "memory pressure footprint:" in text


def test_footprint_timeout_orders_before_summary_timeout() -> None:
    """The walk budget must fit inside the summary budget it runs under."""
    assert (
        resource_usage.FOOTPRINT_SUMMARY_TIMEOUT_SEC
        < resource_usage.SUMMARY_TIMEOUT_SEC
    )


def test_summary_poll_slow_walk_times_out_without_leak() -> None:
    """A thrashing host must not make the final line wait for the walk.

    The peaks line is the last thing a CI log shows; holding it for a walk
    that never answers would hide the run result behind a stuck snapshot.
    """

    async def _amain():
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def _fake(*, include_processes=False, lag_window_sec=None):
            if include_processes:
                await asyncio.sleep(3600.0)
                return {"processes": {"wm": {}}}
            await asyncio.sleep(0.5)
            return _snapshot(processes=None)

        reporter = resource_usage.RunReporter(_fake, lag_window_sec=30.0)
        snapshot = await reporter.summary_poll()
        elapsed = loop.time() - started
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        return snapshot, elapsed, pending

    with asyncio.Runner(loop_factory=_VirtualTimeLoop) as runner:
        snapshot, elapsed, pending = runner.run(_amain())

    assert elapsed < resource_usage.FOOTPRINT_SUMMARY_TIMEOUT_SEC + 0.5 + 1.0
    assert snapshot["processes"] == {
        "error": f"no answer within {resource_usage.FOOTPRINT_SUMMARY_TIMEOUT_SEC}s"
    }
    assert pending == []


async def test_summary_poll_fast_failure_reaps_walk() -> None:
    """A failing fast call must not leave its walk running behind."""

    async def _fake(*, include_processes=False, lag_window_sec=None):
        if include_processes:
            await asyncio.sleep(3600.0)
            return {"processes": {}}
        raise RuntimeError("fast boom")

    reporter = resource_usage.RunReporter(_fake, lag_window_sec=30.0)
    with pytest.raises(RuntimeError, match="fast boom"):
        await reporter.summary_poll()
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


async def test_summary_poll_walk_failure_reports_error() -> None:
    """A walk that fails fast must read as an error footprint, not a crash."""

    async def _fake(*, include_processes=False, lag_window_sec=None):
        if include_processes:
            raise RuntimeError("walk boom")
        return _snapshot(processes=None)

    reporter = resource_usage.RunReporter(_fake, lag_window_sec=30.0)
    snapshot = await reporter.summary_poll()

    assert snapshot["processes"] == {"error": "RuntimeError: walk boom"}


async def test_periodic_uses_summary_poll_for_summary() -> None:
    """The exit summary must come from the walk-aware poll, not the tick poll."""
    calls: list[str] = []

    async def _poll() -> dict:
        calls.append("poll")
        return _snapshot()

    async def _summary() -> dict:
        calls.append("summary")
        return _snapshot()

    async with resource_usage.periodic(
        _poll,
        0.02,
        emit=lambda _line: None,
        emit_status=lambda _line: None,
        summary_poll=_summary,
    ):
        await asyncio.sleep(0.05)

    assert calls.count("summary") == 1
    assert calls[-1] == "summary"
    assert "poll" in calls
