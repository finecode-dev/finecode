"""OTLP end-to-end under the documented diagnostic profile.

A real WM boots with `FINECODE_OTLP_ENDPOINT` pointing at an in-process
OTLP/gRPC sink, runs an action that fans out WM->ER and back, and every
record type lands in the sink from both processes: hop spans with stage
events, a phase span, and health summaries. Lag warnings and late-stage
records need a stalled loop on demand, which no outside caller can arrange;
they are covered deterministically by the in-process ER run below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import time
import typing
from pathlib import Path

import pytest

from tests.e2e.conftest import kill_group, sigint_group, start_server, wait_for_port
from tests.e2e.otlp_sink import OtlpSink


def _send_request(sock: socket.socket, method: str, params: dict, req_id: int) -> None:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    ).encode()
    sock.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def _read_response(
    sock: socket.socket, timeout: float = 120.0, expect_id: int | None = None
) -> dict:
    """Read messages until the reply with `expect_id` arrives, skipping
    interleaved notifications."""
    sock.settimeout(timeout)
    while True:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(1)
            if not chunk:
                raise EOFError("connection closed while reading response header")
            raw += chunk
        header_part, body_start = raw.split(b"\r\n\r\n", 1)
        length = int(header_part.split(b"Content-Length: ")[1])
        body = body_start
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                raise EOFError("connection closed while reading response body")
            body += chunk
        message = json.loads(body)
        if expect_id is None or message.get("id") == expect_id:
            return message


def _span_attrs(span: object) -> dict[str, object]:
    attrs: dict[str, object] = {}
    for item in span.attributes:  # type: ignore[union-attr]
        value = item.value
        which = value.WhichOneof("value")
        attrs[item.key] = getattr(value, which) if which else None
    return attrs


def _service_name(resource_spans: object) -> str | None:
    for item in resource_spans.resource.attributes:  # type: ignore[union-attr]
        if item.key == "service.name":
            return item.value.string_value
    return None


def _all_spans(snapshot: dict[str, list[object]]) -> list[tuple[str, object, object]]:
    """(service, span, resource) triples over every received batch."""
    out = []
    for batch in snapshot["traces"]:
        for resource_spans in batch.resource_spans:
            service = _service_name(resource_spans)
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    out.append((service, span, resource_spans))
    return out


def _all_logs(snapshot: dict[str, list[object]]) -> list[tuple[str, object]]:
    out = []
    for batch in snapshot["logs"]:
        for resource_logs in batch.resource_logs:
            service = None
            for item in resource_logs.resource.attributes:
                if item.key == "service.name":
                    service = item.value.string_value
            for scope_logs in resource_logs.scope_logs:
                for record in scope_logs.log_records:
                    out.append((service, record))
    return out


def _wait_for(
    sink: OtlpSink, predicate: typing.Callable[[], bool], timeout: float
) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if predicate():
            return
        time.sleep(1.0)
    raise AssertionError("OTLP condition not met in time")


@pytest.fixture()
def sink() -> typing.Iterator[OtlpSink]:
    receiver = OtlpSink()
    try:
        yield receiver
    finally:
        receiver.stop()


@pytest.fixture()
def otlp_workspace(tmp_path: Path) -> Path:
    """A workspace with one runnable render action, on a copied venv."""
    import shutil

    from tests.e2e.conftest import _repair_copied_venv_path

    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "test-project"\n'
        'version = "0.1.0"\n'
        "\n"
        "[dependency-groups]\n"
        'dev_workspace = ["finecode"]\n'
        "\n"
        "[tool.finecode]\n",
        encoding="utf-8",
    )
    venvs_dir = tmp_path / ".venvs"
    venvs_dir.mkdir()
    current_venv = Path(sys.executable).parent.parent
    new_venv_dir = venvs_dir / "dev_workspace"
    shutil.copytree(current_venv, new_venv_dir, symlinks=True)
    _repair_copied_venv_path(new_venv_dir, current_venv)
    return tmp_path


def _boot_wm(workspace: Path, tmp_path: Path) -> tuple[subprocess.Popen, int]:
    port_file = tmp_path / "wm_port"
    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "10",
        ],
        cwd=workspace,
    )
    deadline = time.monotonic() + 60.0
    port = 0
    while time.monotonic() < deadline:
        if port_file.exists() and port_file.read_text().strip():
            port = int(port_file.read_text().strip())
            break
        time.sleep(0.5)
    assert port, "WM never wrote its port file"
    assert wait_for_port("127.0.0.1", port), "WM never accepted connections"
    return proc, port


def _add_dir_and_run(port: int, project: Path) -> dict:
    with socket.create_connection(("127.0.0.1", port)) as sock:
        _send_request(sock, "workspace/addDir", {"dirPath": str(project)}, 1)
        _read_response(sock, timeout=120.0, expect_id=1)
        _send_request(
            sock,
            "actions/run",
            {
                "actionSource": "fine_envs.DumpConfigAction",
                "project": str(project),
                "params": {
                    "source_file_path": "file:///tmp/dump-in.toml",
                    "project_raw_config": {"tool": {"finecode": {}}},
                    "target_file_path": "file:///tmp/dump-out.toml",
                    "format_output": False,
                },
                "options": {"trigger": "user", "devEnv": "ci"},
            },
            2,
        )
        return _read_response(sock, timeout=180.0, expect_id=2)


def test_otlp_profile_collects_hops_phases_and_health(
    otlp_workspace: Path,
    tmp_path: Path,
    sink: OtlpSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the diagnostic profile the sink receives hop spans with stage
    events, a phase span and health summaries from both processes."""
    monkeypatch.setenv("FINECODE_OTLP_ENDPOINT", f"http://{sink.address}")
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)

    proc, port = _boot_wm(otlp_workspace, tmp_path)
    try:
        result = _add_dir_and_run(port, otlp_workspace)
        assert result.get("result", {}).get("returnCode", 1) == 0, result

        def _complete() -> bool:
            snapshot = sink.snapshot()
            spans = _all_spans(snapshot)
            logs = _all_logs(snapshot)
            services = {service for service, _, _ in spans} | {
                service for service, _ in logs
            }
            if len([s for s in services if s and s.startswith("finecode")]) < 2:
                return False
            names = [span.name for _, span, _ in spans]
            if not any(n == "phase/render_config_dump" for n in names):
                return False
            if not any("telemetry health summary" in str(r.body) for _, r in logs):
                return False
            hops = [s for _, s, _ in spans if "rpc." in (s.name or "")]
            return len(hops) >= 2

        try:
            _wait_for(sink, _complete, timeout=120.0)
        except AssertionError:
            snapshot = sink.snapshot()
            spans = _all_spans(snapshot)
            logs = _all_logs(snapshot)
            raise AssertionError(
                "sink got "
                f"{len(spans)} spans "
                f"{sorted({s.name for _, s, _ in spans})} and "
                f"{len(logs)} logs "
                f"{sorted({str(r.body)[:60] for _, r in logs})}"
            )

        snapshot = sink.snapshot()
        spans = _all_spans(snapshot)
        hops = [(service, s) for service, s, _ in spans if "rpc." in (s.name or "")]
        assert hops, "no hop spans reached the sink"
        for _, span in hops:
            events = [e.name for e in span.events]
            assert any(n.startswith("rpc.stage.") for n in events), (
                span.name,
                events,
            )
            attrs = _span_attrs(span)
            assert "finecode.rpc.connection_id" in attrs
            assert "finecode.rpc.role" in attrs
        assert any(s.name == "phase/render_config_dump" for _, s, _ in spans)

        summaries = [
            (service, r)
            for service, r in _all_logs(snapshot)
            if "telemetry health summary" in str(r.body)
        ]
        assert len({service for service, _ in summaries}) >= 2
    finally:
        sigint_group(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.wait()


def test_always_off_sampler_named_and_sampled_out(
    otlp_workspace: Path,
    tmp_path: Path,
    sink: OtlpSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `OTEL_TRACES_SAMPLER=always_off` both summaries name that sampler."""
    monkeypatch.setenv("FINECODE_OTLP_ENDPOINT", f"http://{sink.address}")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")

    proc, port = _boot_wm(otlp_workspace, tmp_path)
    try:
        _add_dir_and_run(port, otlp_workspace)

        def _complete() -> bool:
            logs = _all_logs(sink.snapshot())
            return (
                sum(1 for _, r in logs if "telemetry health summary" in str(r.body))
                >= 2
            )

        _wait_for(sink, _complete, timeout=120.0)

        summaries = [
            r
            for _, r in _all_logs(sink.snapshot())
            if "telemetry health summary" in str(r.body)
        ]
        assert len(summaries) >= 2
        assert all("always_off" in str(r.body) for r in summaries)
        assert any("lost" in str(r.body) for r in summaries)
    finally:
        sigint_group(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            proc.wait()


@contextlib.asynccontextmanager
async def _er_stack(sink: OtlpSink):
    """A real ER server with real global telemetry setup against the sink."""
    import asyncio as _asyncio
    import typing as _typing
    from pathlib import Path as _Path

    from finecode_jsonrpc.server_transport import ServerStdioTransport

    from finecode_extension_runner import er_server as _er_server
    from finecode_extension_runner import er_telemetry as _er_telemetry
    from finecode_extension_runner import global_state as _global_state
    from opentelemetry import trace as _otel_trace

    _prior_tracer_provider = _otel_trace.get_tracer_provider()

    _global_state.env_name = "test"
    _global_state.project_dir_path = _Path("/tmp/otlp-probe")
    server = _er_server.create_er_server()
    transport = ServerStdioTransport()
    server._session.attach(transport)

    written: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    writer_task = _asyncio.ensure_future(
        transport._write_messages(_typing.cast(typing.Any, _Writer()))
    )
    reader = _asyncio.StreamReader()
    read_task = _asyncio.ensure_future(transport._read_messages(reader))
    try:
        yield (server, transport, reader, written, read_task)
    finally:
        monitor = server._lag_monitor
        server._lag_monitor = None
        if monitor is not None and monitor._task is not None:
            await monitor.stop()
        from finecode_extension_api import phase_timing_runtime

        phase_timing_runtime.set_phase_timing_binding(None)
        # The ER's real `initialize` installs a global TracerProvider, and the
        # SDK forbids overriding it: a later `set_tracer_provider` (e.g. another
        # test module's fixture) would be silently ignored and its spans lost.
        # Reset the once-guard so the saved provider is current again.
        from opentelemetry.util._once import Once as _Once

        _otel_trace._TRACER_PROVIDER = _prior_tracer_provider
        _otel_trace._TRACER_PROVIDER_SET_ONCE = _Once()
        if _er_telemetry._emitter is not None:
            _er_telemetry._emitter.detach()
            _er_telemetry._emitter = None
        reader.feed_eof()
        await _asyncio.gather(read_task, return_exceptions=True)
        writer_task.cancel()
        await _asyncio.gather(writer_task, return_exceptions=True)
        await transport.stop()


def _er_frame(message: dict) -> bytes:
    body = json.dumps(message).encode()
    return (
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
    ).encode() + body


async def _er_request(
    reader: asyncio.StreamReader,
    written: list[bytes],
    method: str,
    params: dict,
    request_id: int,
) -> dict:
    reader.feed_data(
        _er_frame(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
    )
    for _ in range(2000):
        for data in written:
            _header, _, payload = data.decode().partition("\r\n\r\n")
            reply = json.loads(payload)
            if reply.get("id") == request_id:
                return reply
        await asyncio.sleep(0)
    raise AssertionError(f"no reply for {method}")


def _span_attrs_for_log(record: object) -> dict:
    out: dict = {}
    for item in record.attributes:  # type: ignore[union-attr]
        which = item.value.WhichOneof("value")
        out[item.key] = getattr(item.value, which) if which else None
    return out


async def test_er_records_lag_late_phase_and_health_over_otlp(
    sink: OtlpSink,
) -> None:
    """The ER side, deterministically: a loop stall warns and recovers, a
    timed-out caller logs a matched late response, a phase span records
    elapsed and CPU, and health summaries report the loss -- all over OTLP."""
    import time

    from finecode_extension_api.phase_timing import synchronous_phase

    async with _er_stack(sink) as (server, _transport, reader, written, read_task):
        reply = await _er_request(
            reader,
            written,
            "initialize",
            {
                "processId": 1234,
                "capabilities": {},
                "initializationOptions": {
                    "telemetry": {
                        "mode": "otlp",
                        "otlpEndpoint": f"http://{sink.address}",
                    }
                },
            },
            1,
        )
        assert reply["result"]["telemetry"] == {
            "traces": "active",
            "metrics": "active",
            "logs": "active",
        }

        time.sleep(1.6)  # noqa: ASYNC251 - blocking the loop is the behaviour under test
        await asyncio.sleep(1.2)

        with synchronous_phase(
            "probe_work", input_counts=lambda: {"items": 2}
        ) as phase:
            phase.completion_counts(lambda: {"output_chars": 5})

        caller = asyncio.ensure_future(
            server._session.send_request("finecodeRunner/getInfo", timeout=0.5)
        )
        await asyncio.sleep(1.0)
        assert caller.done()
        with contextlib.suppress(asyncio.TimeoutError):
            caller.result()
        assert not read_task.done()
        pending = [d for d in written]
        assert pending, "the caller request was never sent"
        _header, _, payload = pending[-1].decode().partition("\r\n\r\n")
        outgoing = json.loads(payload)
        for _ in range(3):
            reader.feed_data(
                _er_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": outgoing["id"],
                        "result": {"logFilePath": None},
                    }
                )
            )
            await asyncio.sleep(6.0)
            if any(
                "late_response" in str(r.body) for _, r in _all_logs(sink.snapshot())
            ):
                break

        live = await _er_request(reader, written, "finecodeRunner/getInfo", {}, 999)
        assert live["result"] == {"logFilePath": None}

        def _complete() -> bool:
            snapshot = sink.snapshot()
            spans = _all_spans(snapshot)
            logs = _all_logs(snapshot)
            names = [s.name for _, s, _ in spans]
            bodies = [str(r.body) for _, r in logs]
            return (
                any(n == "jsonrpc.client/finecodeRunner/getInfo" for n in names)
                and any(n == "jsonrpc.server/finecodeRunner/getInfo" for n in names)
                and any(n == "phase/probe_work" for n in names)
                and any("late_response" in b for b in bodies)
                and any("lag exceeded threshold" in b for b in bodies)
                and any("lag recovered" in b for b in bodies)
                and sum("telemetry health summary" in b for b in bodies) >= 1
            )

        try:
            _wait_for(sink, _complete, timeout=150.0)
        except AssertionError:
            snapshot = sink.snapshot()
            spans = _all_spans(snapshot)
            logs = _all_logs(snapshot)
            raise AssertionError(
                "sink got "
                f"{len(spans)} spans "
                f"{sorted({s.name for _, s, _ in spans})} and "
                f"{len(logs)} logs "
                f"{sorted({str(r.body)[:70] for _, r in logs})}"
            )

        snapshot = sink.snapshot()
        logs = _all_logs(snapshot)
        late = [r for _, r in logs if "late_response" in str(r.body)]
        assert late, "no late_response record reached the sink"
        late_attrs = _span_attrs_for_log(late[0])
        assert late_attrs.get("rpc_correlation") == "matched"
        summaries = [r for _, r in logs if "telemetry health summary" in str(r.body)]
        assert any("lost" in str(r.body) for r in summaries)
