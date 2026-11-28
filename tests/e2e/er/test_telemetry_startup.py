"""Startup telemetry over the real ER handler stack.

These tests drive a real `create_er_server()` through a real stdio transport
in-process: the `initialize` exchange carries the telemetry options, the ER
makes exactly one setup attempt, and later requests are traced. Spawning a
real ER subprocess would add nothing: the CLI entry gives no port to connect
to, and everything the delivery needs to observe happens inside this exchange.
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import json
import socket
import time
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest
from finecode_extension_runner.shared_telemetry.health import HealthTracker
from finecode_jsonrpc.server_transport import ServerStdioTransport
from loguru import logger
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from finecode_extension_runner import er_server, er_telemetry, global_state


def _frame(message: dict[str, typing.Any]) -> bytes:
    body = json.dumps(message).encode()
    return (
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
    ).encode() + body


def _body(data: bytes) -> dict[str, typing.Any]:
    _header, _, payload = data.decode().partition("\r\n\r\n")
    return typing.cast(dict[str, typing.Any], json.loads(payload))


class _FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


@contextlib.asynccontextmanager
async def _server_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.AsyncGenerator[SimpleNamespace, None]:
    """A real ER server on a real transport with hermetic telemetry globals."""
    from finecode_extension_runner.shared_telemetry import providers as shared_providers
    from opentelemetry import trace as otel_trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _test_tracer(name: str, *args: object, **kwargs: object) -> object:
        return provider.get_tracer(name)

    monkeypatch.setattr(otel_trace, "get_tracer", _test_tracer)
    health = HealthTracker(process_role="er")
    monkeypatch.setattr(er_telemetry, "_health", health)
    monkeypatch.setattr(er_telemetry, "_telemetry_initialized", False)
    monkeypatch.setattr(er_telemetry, "_emitter", None)
    monkeypatch.setattr(er_telemetry, "_hop_observer", None)
    monkeypatch.setattr(global_state, "env_name", "test")
    monkeypatch.setattr(global_state, "project_dir_path", Path("/tmp/proj"))
    monkeypatch.setattr(shared_providers, "_attempted", set())
    monkeypatch.setattr(shared_providers, "_statuses", {})
    monkeypatch.setattr(shared_providers, "_reasons", {})

    server = er_server.create_er_server()
    transport = ServerStdioTransport()
    server._session.attach(transport)
    writer = _FakeWriter()
    writer_task = asyncio.ensure_future(
        transport._write_messages(typing.cast(typing.Any, writer))
    )
    reader = asyncio.StreamReader()
    read_task = asyncio.ensure_future(transport._read_messages(reader))
    state = SimpleNamespace(
        server=server,
        transport=transport,
        writer=writer,
        reader=reader,
        exporter=exporter,
        health=health,
        next_id=0,
    )
    try:
        yield state
    finally:
        monitor = server._lag_monitor
        server._lag_monitor = None
        if monitor is not None and monitor._task is not None:
            await monitor.stop()
        from finecode_extension_api import phase_timing_runtime

        phase_timing_runtime.set_phase_timing_binding(None)
        if er_telemetry._emitter is not None:
            er_telemetry._emitter.detach()
            er_telemetry._emitter = None
        reader.feed_eof()
        await asyncio.gather(read_task, return_exceptions=True)
        _ = writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)
        await transport.stop()


async def _request(
    state: SimpleNamespace, method: str, params: dict[str, typing.Any] | None
) -> dict[str, typing.Any]:
    """Send one request and wait for its reply, matched by id."""
    state.next_id += 1
    request_id = state.next_id
    state.reader.feed_data(
        _frame(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
    )
    for _ in range(2000):
        for data in state.writer.written:
            reply = _body(data)
            if reply.get("id") == request_id:
                return reply
        await asyncio.sleep(0)
    raise AssertionError(f"no reply for {method}")


def _no_global_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real setup without installing process-global providers.

    Span creation still uses the fixture's in-memory provider, so the hop
    fallback path keeps working; only the global installation is skipped.
    """
    from loguru import logger
    from opentelemetry import metrics as otel_metrics
    from opentelemetry import trace as otel_trace

    def _no_provider(provider: object) -> None:
        return None

    def _no_sink(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(otel_metrics, "set_meter_provider", _no_provider)
    monkeypatch.setattr(otel_trace, "set_tracer_provider", _no_provider)
    monkeypatch.setattr(logger, "add", _no_sink)


def _initialize_params(
    telemetry: dict[str, typing.Any] | None,
) -> dict[str, typing.Any]:
    params: dict[str, typing.Any] = {
        "processId": 1234,
        "capabilities": {},
        "clientInfo": {"name": "telemetry-startup-test", "version": "1"},
    }
    if telemetry is not None:
        params["initializationOptions"] = {"telemetry": telemetry}
    return params


async def test_initialize_carries_options_and_reports_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `initialize` request carries the telemetry options; the result
    reports per-signal status and a repeat returns the stored result."""
    async with _server_stack(monkeypatch) as stack:
        _no_global_install(monkeypatch)
        calls: list[dict[str, typing.Any]] = []
        from finecode_extension_runner.shared_telemetry import (
            providers as shared_providers,
        )

        real_setup = shared_providers.setup_providers

        def _counting(**kwargs: typing.Any) -> typing.Any:
            calls.append(kwargs)
            return real_setup(**kwargs)

        monkeypatch.setattr(shared_providers, "setup_providers", _counting)

        reply = await _request(
            stack,
            "initialize",
            _initialize_params({"mode": "otlp", "otlpEndpoint": "http://127.0.0.1:1"}),
        )
        assert reply["result"]["telemetry"] == {
            "traces": "active",
            "metrics": "active",
            "logs": "active",
        }
        assert len(calls) == 1
        assert calls[0]["endpoint"] == "http://127.0.0.1:1"

        again = await _request(
            stack,
            "initialize",
            _initialize_params({"mode": "otlp", "otlpEndpoint": "http://127.0.0.1:1"}),
        )
        assert again["result"] == reply["result"]
        assert len(calls) == 1


async def test_initialize_disabled_disables_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled value sets every signal disabled with no setup attempt."""
    async with _server_stack(monkeypatch) as stack:
        reply = await _request(
            stack, "initialize", _initialize_params({"mode": "disabled"})
        )
        assert reply["result"]["telemetry"] == {
            "traces": "disabled",
            "metrics": "disabled",
            "logs": "disabled",
        }
        assert stack.server._lag_monitor is None


async def test_unparsable_endpoint_fails_signals_not_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparsable endpoint marks every signal unavailable with a reason;
    the monitor still activates."""
    async with _server_stack(monkeypatch) as stack:
        reply = await _request(
            stack,
            "initialize",
            _initialize_params({"mode": "otlp", "otlpEndpoint": "http://localhost"}),
        )
        assert reply["result"]["telemetry"] == {
            "traces": "unavailable",
            "metrics": "unavailable",
            "logs": "unavailable",
        }
        assert stack.server._lag_monitor is not None
        assert stack.server._lag_monitor.availability == "active"


async def test_unreachable_endpoint_does_not_delay_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup never probes the collector: a closed port answers fast."""
    async with _server_stack(monkeypatch) as stack:
        _no_global_install(monkeypatch)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        started = time.monotonic()
        reply = await _request(
            stack,
            "initialize",
            _initialize_params(
                {"mode": "otlp", "otlpEndpoint": f"http://127.0.0.1:{port}"}
            ),
        )
        elapsed = time.monotonic() - started

        assert reply["result"]["telemetry"] == {
            "traces": "active",
            "metrics": "active",
            "logs": "active",
        }
        assert elapsed < 10.0


async def test_getinfo_after_initialize_is_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests after `initialize` produce receiver hop records."""
    async with _server_stack(monkeypatch) as stack:
        _no_global_install(monkeypatch)
        await _request(
            stack,
            "initialize",
            _initialize_params({"mode": "otlp", "otlpEndpoint": "http://127.0.0.1:1"}),
        )

        reply = await _request(stack, "finecodeRunner/getInfo", {})
        assert "result" in reply

        by_name: dict[str, list[typing.Any]] = {}
        for span in stack.exporter.get_finished_spans():
            by_name.setdefault(span.name, []).append(span)
        assert "jsonrpc.server/initialize" in by_name
        assert "jsonrpc.server/finecodeRunner/getInfo" in by_name
        info_span = by_name["jsonrpc.server/finecodeRunner/getInfo"][0]
        assert info_span.attributes["finecode.rpc.handler_outcome"] == "returned"
        assert info_span.attributes["finecode.rpc.close_reason"] == "completed"


async def test_credential_bearing_endpoint_never_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the response nor any log carries the secret, the endpoint, or
    a setup traceback when a provider fails."""
    async with _server_stack(monkeypatch) as stack:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        endpoint = f"http://user:s3cr3t@127.0.0.1:{port}"
        _no_global_install(monkeypatch)

        from finecode_extension_runner.shared_telemetry import (
            providers as shared_providers,
        )

        def _raise(**kwargs: typing.Any) -> typing.NoReturn:
            raise RuntimeError("s3cr3t")

        monkeypatch.setattr(shared_providers, "_setup_traces", _raise)
        records: list = []
        sink_id = logger.add(lambda message: records.append(message.record))
        try:
            reply = await _request(
                stack,
                "initialize",
                _initialize_params({"mode": "otlp", "otlpEndpoint": endpoint}),
            )
        finally:
            logger.remove(sink_id)

        assert reply["result"]["telemetry"]["traces"] == "unavailable"
        text = json.dumps(reply["result"])
        assert "s3cr3t" not in text
        assert "user" not in text
        for record in records:
            blob = record["message"] + json.dumps(record["extra"], default=str)
            assert "s3cr3t" not in blob, record["message"]
            assert endpoint not in blob, record["message"]
