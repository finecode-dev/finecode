"""Wire records: denial parsing, block IDs and control frame builders."""

from __future__ import annotations

import json

from fine_agent_claude_code import claude_code_control, claude_code_stream


def test_denial_with_tool_use_id() -> None:
    """A denial keeps the tool name and the tool-use ID that was denied."""
    frame = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "nope",
        "permission_denials": [
            {"tool_name": "Write", "tool_use_id": "toolu_014SUvGXkhiRBNN4o6uCxe9U"}
        ],
    }

    result = claude_code_stream.parse_result(frame, None)

    assert result.denials == (
        claude_code_stream.Denial("Write", "toolu_014SUvGXkhiRBNN4o6uCxe9U"),
    )
    assert tuple(d.tool_name for d in result.denials) == ("Write",)


def test_denial_without_tool_use_id() -> None:
    """A denial without an ID still names the tool and records no ID."""
    frame = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "nope",
        "permission_denials": [{"tool_name": "Edit"}],
    }

    result = claude_code_stream.parse_result(frame, None)

    assert result.denials == (claude_code_stream.Denial("Edit", None),)


def test_block_ids_from_recorded_frames() -> None:
    """Assistant tool-use IDs and user tool-result IDs are extracted."""
    assistant = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01TUmttuueb8QvPBJiBkX7p4",
                    "name": "AskUserQuestion",
                    "input": {"q": 1},
                }
            ]
        },
    }
    user = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01TUmttuueb8QvPBJiBkX7p4",
                    "content": "ok",
                }
            ]
        },
    }

    assert claude_code_stream.tool_use_ids(assistant) == [
        "toolu_01TUmttuueb8QvPBJiBkX7p4"
    ]
    assert claude_code_stream.tool_result_ids(user) == [
        "toolu_01TUmttuueb8QvPBJiBkX7p4"
    ]


def test_builders_match_recorded_shapes() -> None:
    """Each builder emits the recorded M1 field shape."""
    init = json.loads(claude_code_control.initialize_line("req_1_ab12cd34"))
    assert init == {
        "type": "control_request",
        "request_id": "req_1_ab12cd34",
        "request": {"subtype": "initialize", "hooks": None},
    }

    user = json.loads(claude_code_control.user_line("do it"))
    assert user == {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": "do it"},
        "parent_tool_use_id": None,
    }

    allow = json.loads(
        claude_code_control.allow_line(
            "rid-1", {"questions": [], "answers": {"q": "a"}}
        )
    )
    assert allow == {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "rid-1",
            "response": {
                "behavior": "allow",
                "updatedInput": {"questions": [], "answers": {"q": "a"}},
            },
        },
    }

    deny = json.loads(claude_code_control.deny_line("rid-2", "nope"))
    assert deny["response"]["response"] == {"behavior": "deny", "message": "nope"}
    assert deny["response"]["subtype"] == "success"

    err = json.loads(claude_code_control.error_line("rid-3", "boom"))
    assert err == {
        "type": "control_response",
        "response": {"subtype": "error", "request_id": "rid-3", "error": "boom"},
    }
