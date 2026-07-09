from typing import Any, Callable

from finecode.wm_server.services.log_delivery import (
    ClientLogRecord,
    LogBatcher,
    SubscriptionRegistry,
    build_log_notification,
    level_value,
    redact,
)

RecordedCall = tuple[Any, list[dict[str, Any]], int]


class FakeClock:
    """Deterministic stand-in for time.monotonic, advanced explicitly by tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def _make_record(
    message: str,
    level: str = "INFO",
    group: str = "finecode.wm_server.test",
) -> ClientLogRecord:
    return ClientLogRecord(
        timestamp=0.0, level=level, source="wm", group=group, message=message
    )


def _make_recorder() -> (
    tuple[list[RecordedCall], Callable[[Any, list[dict[str, Any]], int], None]]
):
    calls: list[RecordedCall] = []

    def callback(conn: Any, records: list[dict[str, Any]], dropped: int) -> None:
        calls.append((conn, records, dropped))

    return calls, callback


# --- C1: level_value -------------------------------------------------------


def test_level_value_known_names_map_to_expected_ints() -> None:
    """level_value must return the exact numeric severities the ADR-0030 levels
    rely on, or client-side minLevel filtering silently misroutes records
    (e.g. an operator who subscribed at WARNING would see DEBUG noise, or
    miss an ERROR entirely)."""
    assert level_value("TRACE") == 5
    assert level_value("DEBUG") == 10
    assert level_value("INFO") == 20
    assert level_value("SUCCESS") == 25
    assert level_value("WARNING") == 30
    assert level_value("ERROR") == 40
    assert level_value("CRITICAL") == 50


def test_level_value_is_case_insensitive() -> None:
    """A lowercase level name resolves to the same value as its canonical
    upper-case form, so callers need not normalize case before subscribing
    or emitting."""
    assert level_value("error") == 40
    assert level_value("ERROR") == 40


def test_level_value_unknown_name_defaults_to_info() -> None:
    """An unrecognized level name never raises and falls back to INFO, so a
    malformed level string cannot crash the WM logging sink."""
    assert level_value("bogus") == 20


# --- C2: redact --------------------------------------------------------------


def test_redact_token_with_equals_separator() -> None:
    """A token value after '=' is replaced so a raw credential logged during
    a connection attempt never reaches a subscribed client."""
    assert (
        redact("connecting with token=abc123 to srv")
        == "connecting with token=***REDACTED*** to srv"
    )


def test_redact_password_with_colon_separator() -> None:
    """A password value after ':' is replaced, covering the common
    structured-log format used across handlers."""
    assert redact("password: hunter2") == "password: ***REDACTED***"


def test_redact_authorization_stops_at_first_whitespace() -> None:
    """Redaction removes only the value token immediately after the
    separator; text beyond the first whitespace is preserved verbatim, so
    the redaction pass does not destroy unrelated log context."""
    assert redact("authorization: Bearer xyz") == "authorization: ***REDACTED*** xyz"


def test_redact_quoted_json_key_value() -> None:
    """A quoted JSON value is redacted while the surrounding quotes are
    preserved, so a redacted structured-log line remains valid-looking
    JSON for downstream tooling."""
    assert redact('{"api_key": "sk-1234"}') == '{"api_key": "***REDACTED***"}'


def test_redact_secret_before_comma_preserves_next_field() -> None:
    """The value stops at the next comma so a non-sensitive field following
    a secret on the same line is not accidentally swallowed by redaction."""
    assert redact("secret=s3cr3t,next=ok") == "secret=***REDACTED***,next=ok"


def test_redact_message_without_sensitive_keyword_is_unchanged() -> None:
    """A message containing no sensitive keyword passes through unchanged,
    so ordinary operational logs are not mangled by the redaction pass."""
    assert redact("user logged in as alice") == "user logged in as alice"


def test_redact_keyword_without_separator_is_unchanged() -> None:
    """A sensitive keyword with no ':' or '=' after it is left untouched,
    avoiding a false positive that would corrupt legitimate log text."""
    assert redact("the token is fine") == "the token is fine"


def test_redact_is_idempotent() -> None:
    """Redacting an already-redacted message produces the same output, so a
    line passed through the redaction step twice (e.g. on a retry path)
    cannot be double-mangled."""
    once = redact("password: hunter2")
    assert redact(once) == once


def test_redact_multiple_secrets_in_one_message() -> None:
    """Every sensitive value on a line is redacted, not just the first, so a
    single log line cannot leak a second credential alongside the first."""
    assert redact("token=a secret=b") == "token=***REDACTED*** secret=***REDACTED***"


# --- C3: ClientLogRecord.to_wire ---------------------------------------------


def test_to_wire_returns_camel_case_dict_with_all_fields() -> None:
    """to_wire must expose every field, keyed by its camelCase wire name, or
    the CLI/CI client cannot render the record it receives over the
    server/logRecords notification."""
    record = ClientLogRecord(
        timestamp=1699999999.123,
        level="ERROR",
        source="wm",
        group="finecode.wm_server.runner.runner_manager",
        message="connection lost",
    )
    assert record.to_wire() == {
        "timestamp": 1699999999.123,
        "level": "ERROR",
        "source": "wm",
        "group": "finecode.wm_server.runner.runner_manager",
        "message": "connection lost",
    }


# --- C4: SubscriptionRegistry -------------------------------------------------


def test_empty_registry_has_no_subscribers() -> None:
    """A freshly created registry reports no subscribers, so the WM log
    sink can cheaply skip formatting work when nobody is listening."""
    registry = SubscriptionRegistry()
    assert registry.has_subscribers() is False


def test_empty_registry_min_level_value_is_none() -> None:
    """With no subscriber registered there is no meaningful minimum level,
    letting callers distinguish 'nobody subscribed' from 'subscribed at the
    most verbose level'."""
    registry = SubscriptionRegistry()
    assert registry.min_level_value() is None


def test_empty_registry_subscribers_for_returns_empty_list() -> None:
    """Querying subscribers for any level on an empty registry returns an
    empty list rather than raising, so the delivery path never needs a
    special case for the no-subscribers state."""
    registry = SubscriptionRegistry()
    assert registry.subscribers_for("ERROR") == []


def test_info_subscriber_excluded_from_debug_records() -> None:
    """A subscriber that asked for INFO and above never receives DEBUG
    records, keeping their client output at the verbosity they opted
    into."""
    registry = SubscriptionRegistry()
    registry.register("conn_info", "INFO")
    assert registry.subscribers_for("DEBUG") == []


def test_info_subscriber_included_in_info_level_records() -> None:
    """A subscriber receives records exactly at their subscribed level, not
    just those strictly above it."""
    registry = SubscriptionRegistry()
    registry.register("conn_info", "INFO")
    assert registry.subscribers_for("INFO") == ["conn_info"]


def test_info_subscriber_included_in_warning_level_records() -> None:
    """A subscriber receives records above their subscribed level, so a
    severity escalation is never silently withheld from an existing
    subscription."""
    registry = SubscriptionRegistry()
    registry.register("conn_info", "INFO")
    assert registry.subscribers_for("WARNING") == ["conn_info"]


def test_two_subscribers_min_level_value_is_lowest() -> None:
    """min_level_value reflects the most verbose active subscriber, so the
    sink knows the lowest level it must still bother building records
    for."""
    registry = SubscriptionRegistry()
    registry.register("conn_info", "INFO")
    registry.register("conn_debug", "DEBUG")
    assert registry.min_level_value() == 10


def test_subscribers_for_debug_returns_only_debug_subscriber() -> None:
    """When both an INFO and a DEBUG subscriber exist, only the DEBUG
    subscriber gets DEBUG records — the INFO subscriber is never sent data
    below the verbosity it asked for."""
    registry = SubscriptionRegistry()
    registry.register("conn_info", "INFO")
    registry.register("conn_debug", "DEBUG")
    assert registry.subscribers_for("DEBUG") == ["conn_debug"]


def test_reregistering_conn_updates_level_without_duplicate() -> None:
    """Registering the same connection twice updates its level in place
    instead of adding a second subscription, preventing a client from
    receiving every record twice."""
    registry = SubscriptionRegistry()
    registry.register("conn", "INFO")
    registry.register("conn", "DEBUG")
    assert registry.min_level_value() == 10
    assert registry.subscribers_for("DEBUG") == ["conn"]


def test_unregister_removes_subscriber() -> None:
    """Unregistering a connection stops it from receiving further records,
    so a client that unsubscribed cannot keep getting notifications after
    the fact."""
    registry = SubscriptionRegistry()
    registry.register("conn", "INFO")
    registry.unregister("conn")
    assert registry.has_subscribers() is False


def test_unregister_absent_conn_is_noop() -> None:
    """Unregistering a connection that was never subscribed does not raise,
    so disconnect handling can call unregister unconditionally."""
    registry = SubscriptionRegistry()
    registry.unregister("never_registered")
    assert registry.has_subscribers() is False


# --- C5: LogBatcher -----------------------------------------------------------


def test_tick_before_interval_elapsed_emits_nothing() -> None:
    """Ticking before the coalescing interval has elapsed must not flush, or
    the batching interval degenerates into per-record delivery, defeating
    the point of coalescing."""
    clock = FakeClock()
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=clock)
    batcher.enqueue("conn", _make_record("first"))
    batcher.enqueue("conn", _make_record("second"))
    batcher.tick()
    assert calls == []


def test_tick_after_interval_elapsed_flushes_buffered_records_in_order() -> None:
    """Once the interval elapses, a tick flushes exactly the buffered
    records for that connection in enqueue order, so a client's log stream
    stays chronological."""
    clock = FakeClock()
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=clock)
    batcher.enqueue("conn", _make_record("first"))
    batcher.enqueue("conn", _make_record("second"))
    clock.advance(0.25)
    batcher.tick()
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert [r["message"] for r in records] == ["first", "second"]
    assert dropped == 0


def test_flushing_one_conn_does_not_emit_other_conns_buffer() -> None:
    """Flushing one subscriber's buffer never leaks another subscriber's
    buffered records, so two clients' log streams are never
    cross-contaminated."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=FakeClock())
    batcher.enqueue("conn_a", _make_record("a-message"))
    batcher.enqueue("conn_b", _make_record("b-message"))
    batcher.flush("conn_a")
    assert len(calls) == 1
    conn, records, _dropped = calls[0]
    assert conn == "conn_a"
    assert [r["message"] for r in records] == ["a-message"]


def test_enqueue_at_max_batch_flushes_immediately() -> None:
    """A connection's buffer is flushed as soon as it reaches max_batch,
    without waiting for a tick, so a burst of records cannot pile up
    unbounded before it reaches the client."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, max_batch=3, now=FakeClock())
    batcher.enqueue("conn", _make_record("one"))
    batcher.enqueue("conn", _make_record("two"))
    batcher.enqueue("conn", _make_record("three"))
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert len(records) == 3
    assert dropped == 0


def test_enqueue_error_record_flushes_immediately_with_prior_buffered_records() -> None:
    """An ERROR record flushes its connection immediately, carrying along
    any lower-severity records already buffered, so an operator sees the
    error without a batching delay and without losing the context that
    preceded it."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=FakeClock())
    batcher.enqueue("conn", _make_record("context", level="INFO"))
    batcher.enqueue("conn", _make_record("boom", level="ERROR"))
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert [r["message"] for r in records] == ["context", "boom"]
    assert dropped == 0


def test_enqueue_critical_record_flushes_immediately() -> None:
    """A CRITICAL record flushes its connection immediately just like
    ERROR, so the most severe failures are never held back by the
    coalescing interval."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=FakeClock())
    batcher.enqueue("conn", _make_record("meltdown", level="CRITICAL"))
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert [r["message"] for r in records] == ["meltdown"]
    assert dropped == 0


def test_buffer_limit_drops_oldest_records_beyond_limit() -> None:
    """When a connection's buffer exceeds the hard cap, the oldest records
    are dropped rather than the newest, so a client that eventually
    catches up sees the most recent, most relevant state instead of stale
    history."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, buffer_limit=2, now=FakeClock())
    batcher.enqueue("conn", _make_record("r1"))
    batcher.enqueue("conn", _make_record("r2"))
    batcher.enqueue("conn", _make_record("r3"))
    batcher.enqueue("conn", _make_record("r4"))
    assert calls == []
    batcher.flush("conn")
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert [r["message"] for r in records] == ["r3", "r4"]
    assert dropped == 2


def test_flush_reports_accumulated_dropped_count() -> None:
    """The dropped-record count accumulated between flushes is delivered on
    the next flush, so an operator can tell their view has a gap instead of
    silently missing data."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, buffer_limit=2, now=FakeClock())
    for message in ("r1", "r2", "r3", "r4"):
        batcher.enqueue("conn", _make_record(message))
    batcher.flush("conn")
    _conn, _records, dropped = calls[0]
    assert dropped == 2


def test_dropped_count_resets_to_zero_after_being_reported() -> None:
    """Once a dropped-count has been delivered to the client, it does not
    keep re-appearing on subsequent flushes, so the gap marker is reported
    exactly once instead of confusing the operator with a stale count."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, buffer_limit=2, now=FakeClock())
    for message in ("r1", "r2", "r3", "r4"):
        batcher.enqueue("conn", _make_record(message))
    batcher.flush("conn")
    batcher.enqueue("conn", _make_record("r5"))
    batcher.flush("conn")
    assert len(calls) == 2
    _conn, records, dropped = calls[1]
    assert [r["message"] for r in records] == ["r5"]
    assert dropped == 0


def test_flushing_empty_buffer_without_drops_emits_no_callback() -> None:
    """Flushing a connection with nothing buffered and no pending drops
    produces no notification, so an idle client is never sent a useless
    empty message."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=FakeClock())
    batcher.flush("conn")
    assert calls == []


def test_flushing_empty_buffer_with_pending_drops_emits_drop_marker() -> None:
    """Even when the buffer itself ends up empty, a pending drop count is
    still delivered as an empty-records notification, so a client is never
    left unaware that it lost data."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, buffer_limit=0, now=FakeClock())
    batcher.enqueue("conn", _make_record("will be dropped"))
    batcher.flush("conn")
    assert len(calls) == 1
    conn, records, dropped = calls[0]
    assert conn == "conn"
    assert records == []
    assert dropped == 1


def test_flush_all_flushes_every_conn_with_buffered_data() -> None:
    """flush_all delivers every subscriber's buffered records in one pass,
    so nothing is left stranded when the WM needs to guarantee delivery at
    a request boundary or on shutdown."""
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=FakeClock())
    batcher.enqueue("conn_a", _make_record("a-message"))
    batcher.enqueue("conn_b", _make_record("b-message"))
    batcher.flush_all()
    assert len(calls) == 2
    flushed_conns = {conn for conn, _records, _dropped in calls}
    assert flushed_conns == {"conn_a", "conn_b"}


def test_tick_after_flush_within_new_interval_emits_nothing() -> None:
    """A flush resets that connection's interval anchor, so a tick shortly
    afterward does not re-flush prematurely; without this, a forced flush
    at a request boundary would corrupt the batching cadence for
    everything that follows."""
    clock = FakeClock()
    calls, callback = _make_recorder()
    batcher = LogBatcher(callback, interval_ms=200, now=clock)
    batcher.enqueue("conn", _make_record("first"))
    batcher.flush("conn")
    assert len(calls) == 1
    batcher.enqueue("conn", _make_record("second"))
    clock.advance(0.1)
    batcher.tick()
    assert len(calls) == 1


# --- C6: build_log_notification ------------------------------------------------


def test_build_log_notification_without_drops_omits_dropped_count_key() -> None:
    """The wire payload omits droppedCount entirely when nothing was
    dropped, so an ordinary client does not need to special-case a zero
    value on every notification."""
    records = [{"message": "one"}, {"message": "two"}]
    assert build_log_notification(records, 0) == {"records": records}


def test_build_log_notification_with_drops_includes_dropped_count_key() -> None:
    """The wire payload includes droppedCount whenever records were lost,
    so a client can surface a visible warning instead of silently
    rendering a gap in its log view."""
    assert build_log_notification([], 3) == {"records": [], "droppedCount": 3}
