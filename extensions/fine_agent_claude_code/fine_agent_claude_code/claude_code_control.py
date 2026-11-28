"""Control-wire records and frame builders for question runs.

The wire stops here: this module owns the JSON shapes exchanged over
``--input-format stream-json --permission-prompt-tool stdio``, copied from the
recorded frames, and the handler owns everything decided with them.
"""

from __future__ import annotations

import enum
import json
import typing
import uuid

__all__ = [
    "AskUserQuestionEntry",
    "AskUserQuestionInput",
    "AskUserQuestionOption",
    "CanUseToolRequest",
    "ControlRequestOut",
    "ControlRequestSubtype",
    "ControlResponseFrame",
    "ErrorResponse",
    "InitializeRequest",
    "PermissionAllow",
    "PermissionDeny",
    "SuccessResponse",
    "UserFrame",
    "UserMessage",
    "allow_line",
    "deny_line",
    "error_line",
    "initialize_line",
    "user_line",
]


class ControlRequestSubtype(enum.StrEnum):
    INITIALIZE = "initialize"
    CAN_USE_TOOL = "can_use_tool"


class InitializeRequest(typing.TypedDict):
    subtype: typing.Literal["initialize"]
    hooks: None


class ControlRequestOut(typing.TypedDict):
    type: typing.Literal["control_request"]
    request_id: str
    request: InitializeRequest


class UserMessage(typing.TypedDict):
    role: typing.Literal["user"]
    content: str


class UserFrame(typing.TypedDict):
    type: typing.Literal["user"]
    session_id: str
    message: UserMessage
    parent_tool_use_id: None


class AskUserQuestionOption(typing.TypedDict, total=False):
    label: str
    description: str


class AskUserQuestionEntry(typing.TypedDict, total=False):
    question: str
    header: str
    options: list[AskUserQuestionOption]
    multiSelect: bool


class AskUserQuestionInput(typing.TypedDict, total=False):
    questions: list[AskUserQuestionEntry]


class CanUseToolRequest(typing.TypedDict, total=False):
    subtype: str
    tool_name: str
    display_name: str
    input: dict[str, object]
    tool_use_id: str
    description: str
    permission_suggestions: list[object]
    requires_user_interaction: bool


class PermissionAllow(typing.TypedDict):
    behavior: typing.Literal["allow"]
    updatedInput: dict[str, object]


class PermissionDeny(typing.TypedDict):
    behavior: typing.Literal["deny"]
    message: str


class SuccessResponse(typing.TypedDict):
    subtype: typing.Literal["success"]
    request_id: str
    response: PermissionAllow | PermissionDeny


class ErrorResponse(typing.TypedDict):
    subtype: typing.Literal["error"]
    request_id: str
    error: str


class ControlResponseFrame(typing.TypedDict):
    type: typing.Literal["control_response"]
    response: SuccessResponse | ErrorResponse


def initialize_line(request_id: str | None = None) -> str:
    rid = request_id or f"req_1_{uuid.uuid4().hex[:8]}"
    frame: ControlRequestOut = {
        "type": "control_request",
        "request_id": rid,
        "request": {"subtype": "initialize", "hooks": None},
    }
    return json.dumps(frame) + "\n"


def user_line(prompt: str) -> str:
    frame: UserFrame = {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }
    return json.dumps(frame) + "\n"


def allow_line(request_id: str, updated_input: dict[str, object]) -> str:
    frame: ControlResponseFrame = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": {"behavior": "allow", "updatedInput": updated_input},
        },
    }
    return json.dumps(frame) + "\n"


def deny_line(request_id: str, message: str) -> str:
    frame: ControlResponseFrame = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": {"behavior": "deny", "message": message},
        },
    }
    return json.dumps(frame) + "\n"


def error_line(request_id: str, error: str) -> str:
    frame: ControlResponseFrame = {
        "type": "control_response",
        "response": {"subtype": "error", "request_id": request_id, "error": error},
    }
    return json.dumps(frame) + "\n"
