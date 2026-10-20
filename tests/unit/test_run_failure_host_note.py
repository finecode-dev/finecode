"""Run failures must carry host memory state to the client that sees them.

When the host is thrashing, an ER timeout arrives as a bare ``boom`` with no
hint that the machine was out of memory. The WM reads host state at the
boundary where the failure becomes a client error, logs it as structured
extras every time, and appends it to the message while pressure holds.
"""

from __future__ import annotations

import pytest
from finecode_jsonrpc.client import ServerFailedToStart
from loguru import logger

from finecode.wm_server import host_pressure, wm_server
from finecode.wm_server._api_handlers import _streaming
from finecode.wm_server.errors import ActionRunFailed, StartingEnvironmentsFailed


def _pressured_reading() -> host_pressure.MemoryPressureReading:
    return host_pressure.MemoryPressureReading(
        meminfo=host_pressure.MemInfo(
            mem_total_mb=17920,
            mem_available_mb=545,
            swap_total_mb=20728,
            swap_used_mb=20727,
        ),
        pressure=host_pressure.HostPressure(
            mem_available_mb=545,
            swap_used_mb=20727,
            psi_memory_full_avg10=76.91,
            psi_io_full_avg10=1.25,
            psi_cpu_some_avg10=0.18,
        ),
        reasons=("psi", "memoryExhausted"),
    )


def _calm_reading() -> host_pressure.MemoryPressureReading:
    return host_pressure.MemoryPressureReading(
        meminfo=host_pressure.MemInfo(
            mem_total_mb=17920,
            mem_available_mb=10000,
            swap_total_mb=20728,
            swap_used_mb=0,
        ),
        pressure=host_pressure.HostPressure(
            mem_available_mb=10000,
            swap_used_mb=0,
            psi_memory_full_avg10=0.5,
            psi_io_full_avg10=0.1,
            psi_cpu_some_avg10=0.1,
        ),
        reasons=(),
    )


_PRESSURED_SUFFIX = (
    " [host under memory pressure: memory available=545MB of 17920MB"
    " swap used=20727MB of 20728MB PSI memory full=76.91%]"
)


class _FakeWriter:
    async def drain(self) -> None:
        return None


def _error_of(msg: dict) -> tuple[int, str]:
    error = msg["error"]
    return error["code"], error["message"]


async def _run_wm_request(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> tuple[list[dict], list]:
    written: list[dict] = []

    def _capture(writer: object, msg: dict) -> None:
        written.append(msg)

    monkeypatch.setattr(wm_server, "_write_message", _capture)

    async def _handler(params, ws_context):
        raise exc

    records: list = []
    sink_id = logger.add(lambda message: records.append(message.record))
    try:
        await wm_server._handle_request_task(
            _handler, {}, None, _FakeWriter(), 42, "test-client", "actions/run"
        )
    finally:
        logger.remove(sink_id)
    return written, records


async def test_pressured_failure_carries_host_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pressured host must be named in the error the client receives."""
    monkeypatch.setattr(
        host_pressure, "read_memory_pressure", lambda: _pressured_reading()
    )

    written, records = await _run_wm_request(monkeypatch, ActionRunFailed("boom"))

    assert len(written) == 1
    code, message = _error_of(written[0])
    assert code == -32603
    assert message == f"boom{_PRESSURED_SUFFIX}"


async def test_pressured_failure_is_logged_with_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log line must carry the host state as queryable extras, not just text."""
    reading = _pressured_reading()
    monkeypatch.setattr(host_pressure, "read_memory_pressure", lambda: reading)

    _, records = await _run_wm_request(monkeypatch, ActionRunFailed("boom"))

    errors = [r for r in records if r["level"].name == "ERROR"]
    assert errors
    record = errors[-1]
    assert record["extra"] == reading.fields()
    assert record["extra"]["memory_pressure"] is True
    assert "host: memory available=545MB" in record["message"]


async def test_calm_failure_sends_bare_message_but_still_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A calm host must not decorate the message, but the log keeps the extras."""
    reading = _calm_reading()
    monkeypatch.setattr(host_pressure, "read_memory_pressure", lambda: reading)

    written, records = await _run_wm_request(monkeypatch, ActionRunFailed("boom"))

    assert len(written) == 1
    _, message = _error_of(written[0])
    assert message == "boom"
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert errors
    assert errors[-1]["extra"] == reading.fields()


async def test_reader_failure_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken host reader must never leave the client hanging without a reply."""

    def _raise():
        raise RuntimeError("proc gone")

    monkeypatch.setattr(host_pressure, "read_memory_pressure", _raise)

    written, _ = await _run_wm_request(monkeypatch, ActionRunFailed("boom"))

    assert len(written) == 1
    _, message = _error_of(written[0])
    assert message == "boom"


_STREAMING_BOUNDARIES = [
    (
        "run_with_partial",
        "_handle_run_action_with_partial_results_task",
        "_handle_run_action_with_partial_results",
    ),
    (
        "batch_with_partial",
        "_handle_run_batch_with_partial_results_task",
        "_handle_run_batch_with_partial_results",
    ),
    (
        "run_with_progress",
        "_handle_run_action_with_progress_task",
        "_handle_run_action_with_progress",
    ),
    (
        "batch_with_progress",
        "_handle_run_batch_with_progress_task",
        "_handle_run_batch_with_progress",
    ),
]


@pytest.mark.parametrize("kind,name,inner", _STREAMING_BOUNDARIES)
async def test_streaming_pressured_and_calm(
    monkeypatch: pytest.MonkeyPatch, kind: str, name: str, inner: str
) -> None:
    """Each streaming boundary must behave like the plain dispatch boundary."""

    async def _raise(params, ws_context, writer):
        raise ActionRunFailed("boom")

    monkeypatch.setattr(_streaming, inner, _raise)
    written: list[dict] = []
    monkeypatch.setattr(
        _streaming, "_write_message", lambda writer, msg: written.append(msg)
    )

    monkeypatch.setattr(
        host_pressure, "read_memory_pressure", lambda: _pressured_reading()
    )
    await getattr(_streaming, name)({}, None, _FakeWriter(), 7)
    assert len(written) == 1
    code, message = _error_of(written[0])
    assert code == -32603
    assert message == f"boom{_PRESSURED_SUFFIX}"

    written.clear()
    monkeypatch.setattr(host_pressure, "read_memory_pressure", lambda: _calm_reading())
    await getattr(_streaming, name)({}, None, _FakeWriter(), 7)
    assert len(written) == 1
    _, calm_message = _error_of(written[0])
    assert calm_message == "boom"


@pytest.mark.parametrize("kind,name,inner", _STREAMING_BOUNDARIES)
async def test_streaming_starting_envs_pressured_and_calm(
    monkeypatch: pytest.MonkeyPatch, kind: str, name: str, inner: str
) -> None:
    """Environment boot failures go through the same note as run failures."""

    async def _raise(params, ws_context, writer):
        raise StartingEnvironmentsFailed("no env")

    monkeypatch.setattr(_streaming, inner, _raise)
    written: list[dict] = []
    monkeypatch.setattr(
        _streaming, "_write_message", lambda writer, msg: written.append(msg)
    )

    monkeypatch.setattr(
        host_pressure, "read_memory_pressure", lambda: _pressured_reading()
    )
    await getattr(_streaming, name)({}, None, _FakeWriter(), 7)
    assert len(written) == 1
    _, message = _error_of(written[0])
    assert message == f"no env{_PRESSURED_SUFFIX}"

    written.clear()
    monkeypatch.setattr(host_pressure, "read_memory_pressure", lambda: _calm_reading())
    await getattr(_streaming, name)({}, None, _FakeWriter(), 7)
    assert len(written) == 1
    _, calm_message = _error_of(written[0])
    assert calm_message == "no env"


@pytest.mark.parametrize(
    "exc,message",
    [
        (ActionRunFailed("boom"), f"boom{_PRESSURED_SUFFIX}"),
        (StartingEnvironmentsFailed("no env"), f"no env{_PRESSURED_SUFFIX}"),
        (ServerFailedToStart("er boot stalled"), f"er boot stalled{_PRESSURED_SUFFIX}"),
    ],
)
async def test_wm_boundaries_pressured(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, message: str
) -> None:
    """All three failure kinds at the plain boundary carry the note when pressured."""
    monkeypatch.setattr(
        host_pressure, "read_memory_pressure", lambda: _pressured_reading()
    )

    written, _ = await _run_wm_request(monkeypatch, exc)

    assert len(written) == 1
    code, actual = _error_of(written[0])
    assert code == -32603
    assert actual == message


@pytest.mark.parametrize(
    "exc,message",
    [
        (ActionRunFailed("boom"), "boom"),
        (StartingEnvironmentsFailed("no env"), "no env"),
        (ServerFailedToStart("er boot stalled"), "er boot stalled"),
    ],
)
async def test_wm_boundaries_calm(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, message: str
) -> None:
    """All three failure kinds at the plain boundary stay bare when calm."""
    monkeypatch.setattr(host_pressure, "read_memory_pressure", lambda: _calm_reading())

    written, _ = await _run_wm_request(monkeypatch, exc)

    assert len(written) == 1
    _, actual = _error_of(written[0])
    assert actual == message
