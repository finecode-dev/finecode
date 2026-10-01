"""The resource-usage command must report without ever hanging or leaking logs."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from click.testing import CliRunner

from finecode.cli_app import resource_usage as resource_usage_lib
from finecode.cli_app.commands import resource_usage_cmd
from finecode.wm_server import wm_lifecycle


class _FakeWm:
    def __init__(self) -> None:
        self._server = None
        self._writer = None
        self.port = 0
        self.received: list[dict] = []
        self.info_result: dict = {
            "logFilePath": None,
            "pid": 1,
            "version": "0.4.0",
            "clients": [],
        }
        self.snapshot: dict | None = None
        self.snapshots: list[dict] | None = None
        self.error_code: int | None = None
        self.error_message: str = "boom"
        self.never_answer: set[str] = set()
        self.answer_counts: dict[str, int] = {}
        self.close_after_first_snapshot = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        self._writer = writer
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                self.received.append(body)
                if "method" not in body or "id" not in body:
                    continue
                method = body["method"]
                if method in self.never_answer:
                    continue
                self.answer_counts[method] = self.answer_counts.get(method, 0) + 1
                if method == "client/initialize":
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": {}})
                elif method == "server/getInfo":
                    self._send(
                        {"jsonrpc": "2.0", "id": body["id"], "result": self.info_result}
                    )
                elif method == "server/getResourceUsage":
                    if self.error_code is not None:
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": body["id"],
                                "error": {
                                    "code": self.error_code,
                                    "message": self.error_message,
                                },
                            }
                        )
                    else:
                        if self.snapshots is not None:
                            index = min(
                                self.answer_counts[method] - 1, len(self.snapshots) - 1
                            )
                            payload = self.snapshots[index]
                        else:
                            payload = self.snapshot
                        self._send(
                            {"jsonrpc": "2.0", "id": body["id"], "result": payload}
                        )
                        if self.close_after_first_snapshot:
                            writer.close()
                            return
                else:
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return

    def _send(self, msg: dict) -> None:
        assert self._writer is not None
        body = json.dumps(msg).encode()
        self._writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


def _snapshot(uptime: float = 10.0, lag_max: int = 100) -> dict:
    return {
        "timestamp": 100.0,
        "wm": {
            "pid": 1234,
            "uptimeSec": uptime,
            "connectedClients": 2,
            "loopLagMs": 3,
            "loopLagMaxMs": lag_max,
            "loopLagPendingMs": 0,
            "loopLagWindowSec": 30.0,
        },
        "projects": {"total": 72, "running": 40, "active": 3},
        "runners": {
            "byStatus": {},
            "running": 40,
            "starting": 2,
            "active": 4,
            "byEnv": {},
        },
        "budget": {"total": 7, "source": "test"},
        "workSlots": {
            "total": 4,
            "used": 4,
            "free": 0,
            "waiting": 7,
            "stallEscape": False,
            "holders": [],
        },
        "startupSlots": {"total": 3, "used": 3, "free": 0, "waiting": 12},
        "inFlightRuns": [],
        "peaks": {
            "runnersRunning": 61,
            "runnersStarting": 14,
            "projectsActive": 9,
            "workSlotsUsed": 5,
            "workSlotsWaiting": 23,
            "startupSlotsWaiting": 40,
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
            "load1m": 9.8,
            "cpuCount": 8,
        },
        "processes": None,
    }


@pytest.fixture
async def fake_wm():
    server = _FakeWm()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_show_text_and_json(fake_wm, monkeypatch, tmp_path) -> None:
    """Text prints the table labels; json parses to the snapshot."""
    fake_wm.snapshot = _snapshot()
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    emitted: list[str] = []
    statuses: list[str] = []
    await resource_usage_cmd.show(
        tmp_path,
        as_json=False,
        watch_sec=None,
        include_processes=False,
        emit=emitted.append,
        emit_status=statuses.append,
    )
    table = "\n".join(emitted)
    for label in (
        "WM pid",
        "projects",
        "runners",
        "by env",
        "work",
        "startup",
        "budget",
        "in flight",
        "host",
        "peaks",
    ):
        assert label in table
    assert "1234" in table

    emitted.clear()
    await resource_usage_cmd.show(
        tmp_path,
        as_json=True,
        watch_sec=None,
        include_processes=False,
        emit=emitted.append,
        emit_status=statuses.append,
    )
    assert json.loads("\n".join(emitted)) == fake_wm.snapshot


async def test_watch_repeats_first_row_per_snapshot(
    fake_wm, monkeypatch, tmp_path
) -> None:
    """Every watch table carries its own snapshot's uptime and lag."""
    fake_wm.snapshots = [
        _snapshot(uptime=10.0, lag_max=100),
        _snapshot(uptime=20.0, lag_max=200),
    ]
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    emitted: list[str] = []
    task = asyncio.create_task(
        resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=0.01,
            include_processes=False,
            emit=emitted.append,
            emit_status=lambda _line: None,
        )
    )
    while len(emitted) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(emitted) >= 2


async def test_watch_first_snapshot_within_one_second(
    fake_wm, monkeypatch, tmp_path
) -> None:
    """The first watch snapshot must not wait for the interval."""
    fake_wm.snapshot = _snapshot()
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    emitted: list[str] = []
    task = asyncio.create_task(
        resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=30,
            include_processes=False,
            emit=emitted.append,
            emit_status=lambda _line: None,
        )
    )
    await asyncio.wait_for(_wait_for_emitted(emitted), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_for_emitted(emitted: list[str]) -> None:
    while not emitted:
        await asyncio.sleep(0.01)


async def test_watch_json_lines_parse_and_no_answer_on_stderr(
    fake_wm, monkeypatch, tmp_path
) -> None:
    """While a later poll hangs, stdout stays JSON and stderr explains."""
    fake_wm.snapshot = _snapshot()

    async def _hang_second(self, reader, writer):
        self._writer = writer
        try:
            while True:
                header = await reader.readline()
                if not header:
                    return
                length = int(header.decode().split(":")[1].strip())
                await reader.readline()
                body = json.loads((await reader.readexactly(length)).decode())
                self.received.append(body)
                if "method" not in body or "id" not in body:
                    continue
                method = body["method"]
                self.answer_counts[method] = self.answer_counts.get(method, 0) + 1
                if method == "client/initialize":
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": {}})
                elif method == "server/getInfo":
                    self._send(
                        {"jsonrpc": "2.0", "id": body["id"], "result": self.info_result}
                    )
                elif method == "server/getResourceUsage":
                    if self.answer_counts[method] == 1:
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": body["id"],
                                "result": self.snapshot,
                            }
                        )
                    else:
                        continue
                else:
                    self._send({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            return

    monkeypatch.setattr(_FakeWm, "_handle", _hang_second)
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    emitted: list[str] = []
    statuses: list[str] = []
    task = asyncio.create_task(
        resource_usage_cmd.show(
            tmp_path,
            as_json=True,
            watch_sec=0.01,
            include_processes=False,
            emit=emitted.append,
            emit_status=statuses.append,
        )
    )
    while not any("no answer" in line for line in statuses):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for line in emitted:
        json.loads(line)
    assert any("no answer for" in line for line in statuses)
    second_requests = [
        msg
        for msg in fake_wm.received
        if msg.get("method") == "server/getResourceUsage"
    ]
    assert len(second_requests) == 2


async def test_unsupported_and_other_errors_mapped(
    fake_wm, monkeypatch, tmp_path
) -> None:
    """Unknown-method and other errors become named failures, not tracebacks."""
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    fake_wm.snapshot = _snapshot()
    fake_wm.error_code = -32601
    with pytest.raises(resource_usage_cmd.ResourceUsageFailed, match="restart-wm"):
        await resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=None,
            include_processes=False,
            emit=lambda _line: None,
            emit_status=lambda _line: None,
        )
    fake_wm.error_code = -32603
    with pytest.raises(
        resource_usage_cmd.ResourceUsageFailed, match="answered with an error"
    ):
        await resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=None,
            include_processes=False,
            emit=lambda _line: None,
            emit_status=lambda _line: None,
        )


async def test_never_answering_connect_or_snapshot_times_out(
    fake_wm, monkeypatch, tmp_path
) -> None:
    """A starved server must fail with did-not-answer, never lost-connection."""
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    monkeypatch.setattr(resource_usage_cmd, "SHOW_TIMEOUT_SEC", 0.05)
    fake_wm.never_answer = {"client/initialize"}
    with pytest.raises(
        resource_usage_cmd.ResourceUsageFailed, match="did not answer"
    ) as excinfo:
        await asyncio.wait_for(
            resource_usage_cmd.show(
                tmp_path,
                as_json=False,
                watch_sec=None,
                include_processes=False,
                emit=lambda _line: None,
                emit_status=lambda _line: None,
            ),
            1.0,
        )
    assert "unknown" in excinfo.value.message

    fake_wm.never_answer = {"server/getResourceUsage"}
    with pytest.raises(resource_usage_cmd.ResourceUsageFailed, match="did not answer"):
        await asyncio.wait_for(
            resource_usage_cmd.show(
                tmp_path,
                as_json=False,
                watch_sec=None,
                include_processes=False,
                emit=lambda _line: None,
                emit_status=lambda _line: None,
            ),
            1.0,
        )


async def test_dead_reporter_loop_mapped(fake_wm, monkeypatch, tmp_path) -> None:
    """A dead reporter loop must fail loudly instead of hanging the watch."""
    fake_wm.snapshot = _snapshot()
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)

    async def _dead(*args, **kwargs):
        raise RuntimeError("loop")

    monkeypatch.setattr(resource_usage_lib, "_run_loop", _dead)
    with pytest.raises(resource_usage_cmd.ResourceUsageFailed, match="reporter failed"):
        await asyncio.wait_for(
            resource_usage_cmd.show(
                tmp_path,
                as_json=False,
                watch_sec=0.01,
                include_processes=False,
                emit=lambda _line: None,
                emit_status=lambda _line: None,
            ),
            1.0,
        )


async def test_broken_pipe_exits_quietly(fake_wm, monkeypatch, tmp_path) -> None:
    """A closed stdout must end the watch without an error."""
    fake_wm.snapshots = [_snapshot(), _snapshot(uptime=20.0)]
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    calls = {"count": 0}
    old_stdout = sys.stdout

    def _flaky_emit(_line: str) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise BrokenPipeError("closed")

    await resource_usage_cmd.show(
        tmp_path,
        as_json=False,
        watch_sec=0.01,
        include_processes=False,
        emit=_flaky_emit,
        emit_status=lambda _line: None,
    )
    assert sys.stdout != old_stdout
    monkeypatch.undo()


async def test_server_going_away_ends_watch(fake_wm, monkeypatch, tmp_path) -> None:
    """A server that closes the connection ends the watch with went-away."""
    fake_wm.snapshot = _snapshot()
    fake_wm.close_after_first_snapshot = True
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: fake_wm.port)
    with pytest.raises(resource_usage_cmd.ResourceUsageFailed, match="went away"):
        await resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=0.01,
            include_processes=False,
            emit=lambda _line: None,
            emit_status=lambda _line: None,
        )


async def test_refused_connection_mapped(monkeypatch, tmp_path) -> None:
    """A server that exits between discovery and connect reads as lost."""
    import socket as _socket

    sock = _socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: free_port)
    with pytest.raises(
        resource_usage_cmd.ResourceUsageFailed, match="lost the connection"
    ):
        await resource_usage_cmd.show(
            tmp_path,
            as_json=False,
            watch_sec=None,
            include_processes=False,
            emit=lambda _line: None,
            emit_status=lambda _line: None,
        )


def test_cli_requires_shared_server_and_validates_watch(monkeypatch) -> None:
    """Usage errors must fail before any WM is touched."""
    from finecode.cli_app import cli as cli_module

    runner = CliRunner()
    result = runner.invoke(cli_module.resource_usage, [])
    assert result.exit_code == 1
    assert "--shared-server" in result.output

    monkeypatch.setattr(wm_lifecycle, "running_port", lambda: None)
    monkeypatch.setattr(
        wm_lifecycle,
        "ensure_running",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")),
    )
    monkeypatch.setattr(
        wm_lifecycle,
        "start_own_server",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")),
    )
    result = runner.invoke(cli_module.resource_usage, ["--shared-server"])
    assert result.exit_code == 1

    async def _unreachable(*args, **kwargs):
        raise AssertionError("show reached")

    monkeypatch.setattr(resource_usage_cmd, "show", _unreachable)
    for bad in ("nan", "0", "601"):
        result = runner.invoke(
            cli_module.resource_usage, ["--shared-server", f"--watch={bad}"]
        )
        assert result.exit_code == 1
        assert "--watch" in result.output


def test_cli_error_mapping_and_logger_stdout(monkeypatch) -> None:
    """Failures exit 1, Ctrl-C exits 0, and logs never touch stdout."""
    import finecode.cli_app.cli as _cli_mod
    from finecode.cli_app import cli as cli_module

    runner = CliRunner()

    async def _raise_failed(*args, **kwargs):
        raise resource_usage_cmd.ResourceUsageFailed("x")

    monkeypatch.setattr(resource_usage_cmd, "show", _raise_failed)
    result = runner.invoke(cli_module.resource_usage, ["--shared-server"])
    assert result.exit_code == 1
    assert "x" in result.output

    async def _raise_keyboard(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(resource_usage_cmd, "show", _raise_keyboard)
    result = runner.invoke(cli_module.resource_usage, ["--shared-server"])
    assert result.exit_code == 0

    recorded: dict = {}

    def _record_logger(*args, **kwargs):
        recorded.update(kwargs)

    async def _noop_show(*args, **kwargs):
        return None

    monkeypatch.setattr(
        _cli_mod.logger_utils.init_logger, "init_logger", _record_logger
    ) if hasattr(
        _cli_mod.logger_utils.init_logger, "init_logger"
    ) else monkeypatch.setattr(
        "finecode.cli_app.cli.logger_utils.init_logger", _record_logger
    )
    monkeypatch.setattr(resource_usage_cmd, "show", _noop_show)
    result = runner.invoke(cli_module.resource_usage, ["--shared-server"])
    assert result.exit_code == 0
    assert recorded.get("stdout") is False
