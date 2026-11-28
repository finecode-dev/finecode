"""Decoder tests for the Codex JSONL wire format.

A misread frame here becomes a wrong outcome downstream -- a diagnostic
mistaken for a terminal failure, or intermediate text mistaken for the final
answer -- so each event kind, malformed field, and counter rule is pinned
without spawning any process.
"""

from __future__ import annotations

import json

from fine_agent_codex import codex_stream
from fine_agent_codex.codex_stream import CodexEventKind


def _line(frame: object) -> str:
    return json.dumps(frame)


def test_blank_malformed_non_object_and_unknown_lines_are_skipped() -> None:
    """Noise on stdout must not end a run that is otherwise fine."""
    assert codex_stream.parse_event("") is None
    assert codex_stream.parse_event("   \n") is None
    assert codex_stream.parse_event("not json") is None
    assert codex_stream.parse_event(_line([1, 2])) is None
    assert codex_stream.parse_event(_line({"no": "type"})) is None
    assert codex_stream.parse_event(_line({"type": 42})) is None
    assert codex_stream.parse_event(_line({"type": "frobnicate"})) is None


def test_thread_started_carries_only_the_thread_id() -> None:
    """The transcript pointer is for humans inspecting afterwards, not progress."""
    event = codex_stream.parse_event(
        _line({"type": "thread.started", "thread_id": "t-1"})
    )
    assert event is not None
    assert event.kind is CodexEventKind.THREAD_STARTED
    assert event.thread_id == "t-1"
    assert event.narrative is None


def test_turn_started_is_generic_progress_not_a_counter() -> None:
    """An exec request turn is not a reported count of internal assistant turns."""
    event = codex_stream.parse_event(_line({"type": "turn.started"}))
    assert event is not None
    assert event.kind is CodexEventKind.TURN_STARTED
    assert event.narrative is not None


def test_item_events_use_fixed_labels_and_never_carry_model_text() -> None:
    """Progress names the kind of work, never the commands, diffs or text."""
    cases = {
        "command_execution": "Running command",
        "file_change": "Applying file changes",
        "mcp_tool_call": "Calling MCP tool",
        "web_search": "Searching the web",
        "todo_list": "Updating plan",
        "reasoning": "Reasoning",
        "agent_message": "Writing answer",
        "error": "Agent reported an error",
    }
    for item_type, label in cases.items():
        for wire_type in ("item.started", "item.updated", "item.completed"):
            event = codex_stream.parse_event(
                _line(
                    {
                        "type": wire_type,
                        "item": {"type": item_type, "text": "secret payload"},
                    }
                )
            )
            assert event is not None, (wire_type, item_type)
            assert event.narrative == label, (wire_type, item_type)
            if wire_type != "item.completed" or item_type != "agent_message":
                assert event.assistant_text is None, (wire_type, item_type)


def test_unknown_item_types_get_a_generic_label() -> None:
    """A CLI addition must not break the handler or leak raw protocol strings."""
    event = codex_stream.parse_event(
        _line({"type": "item.updated", "item": {"type": "future_thing"}})
    )
    assert event is not None
    assert event.narrative == "Processing item"
    assert event.assistant_text is None


def test_only_completed_agent_messages_replace_fallback_text() -> None:
    """Intermediate text is a failure fallback, not the canonical answer."""
    started = codex_stream.parse_event(
        _line(
            {
                "type": "item.started",
                "item": {"type": "agent_message", "text": "partial"},
            }
        )
    )
    assert started is not None
    assert started.assistant_text is None

    completed = codex_stream.parse_event(
        _line(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "partial"},
            }
        )
    )
    assert completed is not None
    assert completed.assistant_text == "partial"

    reasoning = codex_stream.parse_event(
        _line({"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}})
    )
    assert reasoning is not None
    assert reasoning.assistant_text is None


def test_turn_completed_maps_counters_field_by_field() -> None:
    """Usage reports what the backend reported: zero stays zero, booleans and
    absent counters stay unset, and cached input lands on cache reads."""
    event = codex_stream.parse_event(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 7,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 99,
                },
            }
        )
    )
    assert event is not None
    assert event.kind is CodexEventKind.TURN_COMPLETED
    assert event.usage is not None
    assert event.usage.input_tokens == 0
    assert event.usage.cache_read_tokens == 7
    assert event.usage.output_tokens == 3
    assert event.usage.cache_write_tokens is None
    assert event.usage.total_tokens is None
    assert event.usage.approx_cost_usd is None
    assert event.usage.provider is None
    assert event.usage.model is None


def test_turn_completed_rejects_booleans_and_absent_counters() -> None:
    """A stray boolean on the wire must not silently become a token count, and
    a usage shape with nothing countable reports nothing rather than zeros."""
    event = codex_stream.parse_event(
        _line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": True,
                    "output_tokens": "many",
                    "cached_input_tokens": None,
                },
            }
        )
    )
    assert event is not None
    assert event.usage is None

    missing = codex_stream.parse_event(_line({"type": "turn.completed"}))
    assert missing is not None
    assert missing.usage is None


def test_turn_failed_keeps_reported_usage_and_names_a_reason() -> None:
    event = codex_stream.parse_event(
        _line(
            {
                "type": "turn.failed",
                "error": {"message": "boom"},
                "usage": {"input_tokens": 5, "output_tokens": 6},
            }
        )
    )
    assert event is not None
    assert event.kind is CodexEventKind.TURN_FAILED
    assert event.error == "boom"
    assert event.usage is not None
    assert event.usage.input_tokens == 5
    assert event.usage.output_tokens == 6


def test_turn_failed_without_a_message_still_fails() -> None:
    event = codex_stream.parse_event(_line({"type": "turn.failed"}))
    assert event is not None
    assert event.error is not None


def test_error_is_a_diagnostic_not_a_terminal() -> None:
    """Retry diagnostics arrive as errors; a later completion can still succeed."""
    event = codex_stream.parse_event(
        _line({"type": "error", "message": "retrying after a hiccup"})
    )
    assert event is not None
    assert event.kind is CodexEventKind.ERROR
    assert event.error == "retrying after a hiccup"


def test_malformed_known_fields_do_not_crash_the_decoder() -> None:
    event = codex_stream.parse_event(_line({"type": "thread.started", "thread_id": 42}))
    assert event is not None
    assert event.thread_id is None

    broken = codex_stream.parse_event(
        _line({"type": "item.completed", "item": "not-a-dict"})
    )
    assert broken is not None
    assert broken.narrative == "Processing item"
    assert broken.assistant_text is None
