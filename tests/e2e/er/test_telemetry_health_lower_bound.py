"""An ER that dies keeps only what it already wrote.

Summaries are written when emitted, never at shutdown: a SIGKILLed process
runs no exit handlers, flushes nothing, and writes nothing more. The last
summary in its file log is therefore a lower bound on the loss, never an
overstatement. A failing local file sink likewise never stops the work.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import time
import typing
from pathlib import Path


def _frame(message: dict[str, typing.Any]) -> bytes:
    body = json.dumps(message).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


async def _rpc(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict[str, typing.Any],
    request_id: int,
) -> dict[str, typing.Any]:
    writer.write(
        _frame({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    )
    await writer.drain()
    header = await reader.readuntil(b"\r\n\r\n")
    length = int(header.split(b"\r\n")[0].split(b"Content-Length: ")[1])
    payload = await reader.readexactly(length)
    return typing.cast(dict[str, typing.Any], json.loads(payload))


async def _wait_for_log_lines(
    log_dir: Path, pattern: str, count: int, timeout: float
) -> list[str]:
    """Poll every log file in a directory until `count` lines match."""
    started = time.monotonic()
    matched: list[str] = []
    while time.monotonic() - started < timeout:
        matched = []
        for candidate in sorted(log_dir.glob("*.log*")):
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            matched.extend(line for line in lines if re.search(pattern, line))
        if len(matched) >= count:
            return matched
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"only {len(matched)} of {count} {pattern!r} lines under {log_dir}"
    )


async def _serving_address(log_dir: Path, timeout: float) -> tuple[str, int]:
    matched = await _wait_for_log_lines(
        log_dir, r"Serving on \('([^']+)', (\d+)\)", 1, timeout
    )
    found = re.search(r"Serving on \('([^']+)', (\d+)\)", matched[0])
    assert found is not None
    return found.group(1), int(found.group(2))


async def test_sigkill_keeps_only_earlier_summaries(tmp_path: Path) -> None:
    """SIGKILL after loss leaves the earlier summaries and nothing else.

    A real ER subprocess initializes with telemetry on against a closed
    collector: its own hop spans fail to export, the loss is counted, and the
    next summary reports it. Killing the process after that summary leaves the
    file log with the reported loss and nothing written afterwards -- the
    last summary is a lower bound on the loss.
    """
    project_dir = tmp_path / "proj"
    (project_dir / ".venvs" / "test" / "logs" / "runner").mkdir(parents=True)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "finecode_extension_runner",
        "start",
        "--project-path",
        str(project_dir),
        "--env-name",
        "test",
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        log_dir = project_dir / ".venvs" / "test" / "logs" / "runner"
        host, port = await _serving_address(log_dir, timeout=90.0)
        reader, writer = await asyncio.open_connection(host, port)

        reply = await _rpc(
            reader,
            writer,
            "initialize",
            {
                "processId": os.getpid(),
                "capabilities": {},
                "clientInfo": {"name": "kill-test", "version": "1"},
                "initializationOptions": {
                    "telemetry": {"mode": "otlp", "otlpEndpoint": "http://127.0.0.1:1"}
                },
            },
            1,
        )
        assert reply["result"]["telemetry"] == {
            "traces": "active",
            "metrics": "active",
            "logs": "active",
        }
        reply = await _rpc(reader, writer, "finecodeRunner/getInfo", {}, 2)
        assert "result" in reply

        summaries = await _wait_for_log_lines(
            log_dir, r"telemetry health summary", 2, timeout=150.0
        )
        assert "lost" in summaries[-1]
        proc.send_signal(signal.SIGKILL)
        await asyncio.wait_for(proc.wait(), timeout=30)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        with contextlib.suppress(Exception):
            writer.close()

    assert proc.returncode == -signal.SIGKILL


async def test_failing_file_sink_does_not_stop_work(tmp_path: Path) -> None:
    """A local file sink that raises leaves the work -- and the other sinks --
    running."""
    from finecode_extension_runner.er_lag_monitor import ErLagMonitor
    from finecode_extension_runner.shared_telemetry.health import HealthTracker
    from loguru import logger

    def _failing_sink(message: object) -> None:
        raise RuntimeError("disk on fire")

    records: list[dict] = []
    failing_id = logger.add(_failing_sink, level="TRACE")
    capture_id = logger.add(lambda m: records.append(m.record), level="TRACE")
    try:
        monitor = ErLagMonitor(health=HealthTracker(process_role="er"))
        monitor.arm()
        monitor.start()
        await asyncio.sleep(0.6)
        time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
        await asyncio.sleep(1.2)
        await monitor.stop()
    finally:
        logger.remove(failing_id)
        logger.remove(capture_id)

    messages = [r["message"] for r in records]
    assert any(m.startswith("ER event loop lag exceeded threshold") for m in messages)
    assert any(m.startswith("ER event loop lag recovered") for m in messages)
