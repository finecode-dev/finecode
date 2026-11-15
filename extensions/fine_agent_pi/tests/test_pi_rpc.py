"""Unit tests for the pi `get_state` identity projection."""

from __future__ import annotations

from fine_agent_pi import pi_rpc


def test_state_command_carries_the_given_request_id() -> None:
    """The identity check correlates by the id the client chose."""
    import json

    command = json.loads(pi_rpc.session_state_command("req-1"))

    assert command == {"type": "get_state", "id": "req-1"}


def test_identity_parses_a_successful_state_response() -> None:
    """A matching response yields the reported session and file."""
    frame = {
        "type": "response",
        "command": "get_state",
        "id": "req-1",
        "success": True,
        "data": {"sessionId": "abc", "sessionFile": "/tmp/s.jsonl"},
    }

    identity = pi_rpc.parse_session_identity(frame)

    assert identity is not None
    assert identity.request_id == "req-1"
    assert identity.session_id == "abc"
    assert identity.session_file == "/tmp/s.jsonl"
    assert identity.error is None


def test_identity_ignores_unrelated_records() -> None:
    """Session events and other commands are not identity evidence."""
    assert (
        pi_rpc.parse_session_identity({"type": "agent_settled"}) is None
    )
    assert (
        pi_rpc.parse_session_identity(
            {
                "type": "response",
                "command": "get_session_stats",
                "id": "req-1",
                "success": True,
                "data": {},
            }
        )
        is None
    )
    assert pi_rpc.parse_session_identity("not a frame") is None


def test_identity_reports_failure_and_malformed_data_explicitly() -> None:
    """A failed or malformed identity carries an error, never a session."""
    failed = pi_rpc.parse_session_identity(
        {
            "type": "response",
            "command": "get_state",
            "id": "req-1",
            "success": False,
        }
    )
    assert failed is not None
    assert failed.session_id is None
    assert failed.error is not None

    missing = pi_rpc.parse_session_identity(
        {
            "type": "response",
            "command": "get_state",
            "id": "req-1",
            "success": True,
            "data": {},
        }
    )
    assert missing is not None
    assert missing.session_id is None
    assert missing.error is not None
