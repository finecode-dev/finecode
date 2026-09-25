import pytest
from loguru import logger
from opentelemetry import metrics, trace

from finecode import telemetry


def test_validate_endpoint_parses_host_and_port_with_scheme() -> None:
    host, port = telemetry._validate_endpoint("http://otel-lgtm:4317")
    assert (host, port) == ("otel-lgtm", 4317)


def test_validate_endpoint_parses_host_and_port_without_scheme() -> None:
    host, port = telemetry._validate_endpoint("otel-lgtm:4317")
    assert (host, port) == ("otel-lgtm", 4317)


def test_validate_endpoint_rejects_missing_port() -> None:
    with pytest.raises(ValueError, match="expected host and port"):
        telemetry._validate_endpoint("http://otel-lgtm")


def test_validate_endpoint_rejects_host_only_string() -> None:
    with pytest.raises(ValueError, match="expected host and port"):
        telemetry._validate_endpoint("otel-lgtm")


# ---------------------------------------------------------------------------
# init_* no-op guards (PRD-0004-AC7)
# ---------------------------------------------------------------------------
#
# Each init_* function must do nothing observable when its resolved endpoint
# is falsy — this is what makes observability a true opt-in with zero cost
# when unconfigured (PRD-0004-R6).


@pytest.mark.parametrize("endpoint", [None, ""])
def test_init_otel_logging_noop_when_endpoint_falsy(endpoint: str | None) -> None:
    handlers_before = dict(logger._core.handlers)

    telemetry.init_otel_logging("finecode-test", endpoint=endpoint)

    assert dict(logger._core.handlers) == handlers_before


@pytest.mark.parametrize("endpoint", [None, ""])
def test_init_tracer_provider_noop_when_endpoint_falsy(endpoint: str | None) -> None:
    provider_before = trace.get_tracer_provider()

    telemetry.init_tracer_provider("finecode-test", endpoint=endpoint)

    assert trace.get_tracer_provider() is provider_before


@pytest.mark.parametrize("endpoint", [None, ""])
def test_init_meter_provider_noop_when_endpoint_falsy(endpoint: str | None) -> None:
    provider_before = metrics.get_meter_provider()

    telemetry.init_meter_provider("finecode-test", endpoint=endpoint)

    assert metrics.get_meter_provider() is provider_before


# ---------------------------------------------------------------------------
# _probe_endpoint_once — one-time-per-endpoint reachability heads-up
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_probed_endpoints():
    telemetry._probed_endpoints.clear()
    yield
    telemetry._probed_endpoints.clear()


def test_probe_endpoint_once_warns_on_unreachable_endpoint() -> None:
    captured: list[str] = []
    handler_id = logger.add(
        lambda m: captured.append(m.record["message"]), level="WARNING"
    )
    try:
        telemetry._probe_endpoint_once("http://127.0.0.1:1", "127.0.0.1", 1)
    finally:
        logger.remove(handler_id)

    assert len(captured) == 1
    assert "not reachable yet" in captured[0]


def test_probe_endpoint_once_is_deduped_per_endpoint() -> None:
    captured: list[str] = []
    handler_id = logger.add(
        lambda m: captured.append(m.record["message"]), level="WARNING"
    )
    try:
        telemetry._probe_endpoint_once("http://127.0.0.1:1", "127.0.0.1", 1)
        telemetry._probe_endpoint_once("http://127.0.0.1:1", "127.0.0.1", 1)
    finally:
        logger.remove(handler_id)

    assert len(captured) == 1
