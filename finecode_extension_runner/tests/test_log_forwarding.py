from __future__ import annotations

import pytest
from loguru import logger

from finecode_extension_runner import er_server, logs


@pytest.fixture(autouse=True)
def _reset_forwarding_state():
    """Restore module-level forwarding state after each test."""
    prev_enabled = logs._forward_enabled
    prev_level = logs._forward_level
    prev_sender = logs._forward_sender
    yield
    logs._forward_enabled = prev_enabled
    logs._forward_level = prev_level
    logs.set_forward_sender(prev_sender)


def test_should_forward_false_when_forwarding_disabled():
    logs.set_forward_sender(lambda records: None)
    logs.set_log_forwarding(False)

    assert logs.should_forward(logs.LogLevel.ERROR.value) is False


def test_should_forward_respects_level_threshold():
    logs.set_forward_sender(lambda records: None)
    logs.set_log_forwarding(True, "WARNING")

    assert logs.should_forward(logs.LogLevel.INFO.value) is False
    assert logs.should_forward(logs.LogLevel.WARNING.value) is True
    assert logs.should_forward(logs.LogLevel.ERROR.value) is True


def test_forwarding_independent_of_file_log_level():
    """Enabling forwarding at a level lower than the file/stdout filter must
    not change what the ER writes to its own log file."""
    logs.set_default_log_level(logs.LogLevel.INFO)
    original_default_level = logs._default_log_level

    logs.set_forward_sender(lambda records: None)
    logs.set_log_forwarding(True, "DEBUG")

    assert logs._default_log_level == original_default_level


def test_forward_sink_calls_sender_only_when_enabled():
    captured: list[list[dict]] = []
    logs.set_forward_sender(lambda records: captured.append(records))

    logs.set_log_forwarding(False)
    logger.remove()
    handler_id = logger.add(logs._forward_sink, level="TRACE")
    try:
        logger.info("disabled message")
        assert captured == []

        logs.set_log_forwarding(True, "INFO")
        logger.info("enabled message")
    finally:
        logger.remove(handler_id)

    assert len(captured) == 1
    records = captured[0]
    assert len(records) == 1
    record = records[0]
    assert set(record.keys()) == {"timestamp", "level", "group", "message"}
    assert record["level"] == "INFO"
    assert record["message"] == "enabled message"


async def test_update_logging_handler_sets_state_and_returns_empty_dict():
    class _FakeServer:
        pass

    result = await er_server.update_logging(
        _FakeServer(), {"forward": True, "forwardLevel": "WARNING"}
    )

    assert result == {}
    assert logs._forward_enabled is True
    assert logs._forward_level == logs.LogLevel.WARNING
