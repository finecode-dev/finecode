"""Localhost OTLP/gRPC receiver for tests (traces, metrics, logs).

Built on the `opentelemetry-proto` and `grpcio` modules that ship with the
OTLP exporter: no extra test dependency. Use in-process through
:class:`OtlpSink`, or standalone for a manual run::

    python -m tests.e2e.otlp_sink --port 4317 --out /tmp/otel.jsonl
"""

from __future__ import annotations

import argparse
import threading

__all__ = ["OtlpSink"]


class _Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.traces: list[object] = []
        self.metrics: list[object] = []
        self.logs: list[object] = []


class OtlpSink:
    """A localhost OTLP/gRPC endpoint recording every received batch."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        from concurrent import futures

        import grpc
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
        from opentelemetry.proto.collector.metrics.v1 import (
            metrics_service_pb2_grpc,
        )
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        self._recorder = _Recorder()
        recorder = self._recorder

        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

        class _Trace(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request: object, context: object) -> object:
                with recorder._lock:
                    recorder.traces.append(request)
                return trace_service_pb2.ExportTraceServiceResponse()

        class _Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
            def Export(self, request: object, context: object) -> object:
                with recorder._lock:
                    recorder.metrics.append(request)
                return metrics_service_pb2.ExportMetricsServiceResponse()

        class _Logs(logs_service_pb2_grpc.LogsServiceServicer):
            def Export(self, request: object, context: object) -> object:
                with recorder._lock:
                    recorder.logs.append(request)
                return logs_service_pb2.ExportLogsServiceResponse()

        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
            _Trace(), self._server
        )
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(
            _Metrics(), self._server
        )
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(_Logs(), self._server)
        self._port = self._server.add_insecure_port(f"{host}:{port}")
        self._server.start()
        self.address = f"{host}:{self._port}"

    def snapshot(self) -> dict[str, list[object]]:
        """A copy of the batches received so far, by signal."""
        with self._recorder._lock:
            return {
                "traces": list(self._recorder.traces),
                "metrics": list(self._recorder.metrics),
                "logs": list(self._recorder.logs),
            }

    def stop(self) -> None:
        self._server.stop(grace=None)


def _write_jsonl(path: str, snapshot: dict[str, list[object]]) -> None:
    import json

    from google.protobuf.json_format import MessageToDict

    lines = [
        json.dumps({"signal": signal, "body": MessageToDict(batch)})
        for signal, batches in snapshot.items()
        for batch in batches
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")


def main() -> None:
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sink = OtlpSink(port=args.port)
    print(f"OTLP sink on {sink.address}", flush=True)

    stopped = threading.Event()

    def _stop(signum: object, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    stopped.wait()
    snapshot = sink.snapshot()
    sink.stop()
    _write_jsonl(args.out, snapshot)


if __name__ == "__main__":
    main()
