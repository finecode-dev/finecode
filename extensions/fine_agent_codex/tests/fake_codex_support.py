"""Shared test-only scaffolding for the Codex agent handler tests.

`FakeScenario` selects the fake CLI's behavior, and `DriverRecord` is the
readiness record the fake writes once it owns its process: the exact argv the
production builder assembled, what it read from stdin and the schema file, and
the real PIDs the lifecycle tests assert on. Production code never imports
this module.
"""

from __future__ import annotations

import enum
import typing

from fine_agent_codex.codex_stream import JsonValue

__all__ = ["DriverRecord", "FakeScenario"]


class FakeScenario(enum.StrEnum):
    SUCCESS = "success"
    NOISE = "noise"
    FAILED = "failed"
    NO_TERMINAL = "no_terminal"
    NONZERO = "nonzero"
    RETRY = "retry"
    EMPTY = "empty"
    MISSING_ANSWER = "missing_answer"
    STRUCTURED_VALID = "structured_valid"
    STRUCTURED_INVALID = "structured_invalid"
    HANG = "hang"
    IGNORE_TERM = "ignore_term"
    LEAVE_CHILD = "leave_child"
    BAD_UTF8 = "bad_utf8"


class DriverRecord(typing.TypedDict):
    argv: list[str]
    cwd: str
    prompt: str
    schema: JsonValue
    schema_path: str | None
    answer_path: str
    agent_pid: int
    child_pid: int | None
