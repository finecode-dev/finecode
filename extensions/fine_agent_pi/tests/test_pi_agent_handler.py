"""`PiAgentHandler` driving a fake pi over the real RPC wire format.

The fake is a real subprocess speaking real JSONL over real pipes, so these
exercise the parts that actually break -- framing, event sequencing, reply
shapes -- without a model call, a bill, or a nondeterministic answer.

Two of these guard mistakes that pi's own docs invite: `agent_end` looks like
the end of a run and is not, and `confirm` replies with a differently named
field than the other three dialogs.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any

import pytest
from fine_agent import backend_support
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_runner.testing import run_handler

from fine_agent_pi import pi_agent_handler
from fine_agent_pi.pi_agent_handler import PiAgentHandler

_FAKE_PI = pathlib.Path(__file__).parent / "fake_pi.py"


_ACTIVE_SCENARIO: dict[str, Any] = {}


class _FakePiHandler(PiAgentHandler):
    """The real handler with the fake pi swapped in for the `pi` binary.

    Module level because `run_handler` resolves a handler by its import path, so
    a class defined inside a test function cannot be loaded. The scenario
    therefore travels through `_ACTIVE_SCENARIO` rather than a closure -- adding
    an `executable` field to the production config would be the tidier-looking
    option and the wrong one, since pointing the handler at an arbitrary binary
    is not something configuration should be able to do.
    """

    def _executable(self) -> list[str]:
        return [sys.executable, str(_FAKE_PI), json.dumps(_ACTIVE_SCENARIO)]


async def _run(
    scenario: dict[str, Any],
    *,
    prompt: str = "do the thing",
    handler_config: dict[str, Any] | None = None,
    profile: str | None = None,
    output_schema: dict[str, Any] | None = None,
    service_overrides: dict[Any, Any] | None = None,
) -> RunAgentTaskRunResult:
    global _ACTIVE_SCENARIO
    _ACTIVE_SCENARIO = scenario
    try:
        result = await run_handler(
            _FakePiHandler,
            RunAgentTaskRunPayload(
                prompt=prompt, profile=profile, output_schema=output_schema
            ),
            action_cls=RunAgentTaskAction,
            handler_config=handler_config,
            service_overrides=service_overrides,
        )
    finally:
        _ACTIVE_SCENARIO = {}

    # `run_handler` is typed as returning the base result or `None`. Narrowing
    # here rather than in each test keeps the assertions readable, and the
    # isinstance check is itself worth having: a handler that returned the wrong
    # result type would otherwise surface as a confusing attribute error.
    assert isinstance(result, RunAgentTaskRunResult)
    return result


def _records(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _text(delta: str) -> dict[str, Any]:
    return {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": delta},
    }


async def test_assembles_streamed_text_and_settles() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _text("Hello")},
                {"do": "emit", "frame": _text(", world")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "Hello, world"
    assert result.turns == 1
    assert result.error is None


async def test_agent_end_with_will_retry_does_not_end_the_run() -> None:
    """`agent_end` fires per low-level run and again after a transient retry.

    Treating it as completion silently truncates every run that hits a
    rate limit -- the output after the retry would simply be missing, with the
    result still reporting success.
    """
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("partial")},
                {"do": "emit", "frame": {"type": "agent_end", "willRetry": True}},
                {"do": "emit", "frame": _text(" and the rest")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "partial and the rest"


def _failed_turn(
    detail: str = '402: {"message":"Insufficient Balance"}',
    *,
    event: str = "turn_end",
) -> dict[str, Any]:
    """A turn whose model call errored, shaped as pi really shapes it.

    Copied from a real `pi --mode rpc` capture: the error lives only on the
    message, and the run settles normally afterwards.
    """
    return {
        "type": event,
        "message": {
            "role": "assistant",
            "content": [],
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "stopReason": "error",
            "errorMessage": detail,
        },
    }


async def test_provider_error_is_a_failure_not_a_silent_settle() -> None:
    """A model call that never happened must not report success.

    pi signals a provider error only as `stopReason: "error"` on the message
    frames, then emits `agent_settled` and exits 0. Reading `agent_settled`
    alone turns an expired key or an unfunded account into a run that
    "succeeded" with an empty answer -- which is what the caller then has to
    debug, with nothing to go on.
    """
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _failed_turn(event="message_end")},
                {"do": "emit", "frame": _failed_turn()},
                {"do": "emit", "frame": {"type": "agent_end"}},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == ""
    assert "Insufficient Balance" in (result.error or "")
    # The backend is named because it is usually the thing to fix, and pi
    # chooses it itself when FineCode configures neither model nor provider.
    assert "deepseek/deepseek-v4-flash" in (result.error or "")


async def test_error_on_message_end_alone_is_still_reported() -> None:
    """`turn_end` may never arrive if pi dies between the two frames."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _failed_turn(event="message_end")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert "Insufficient Balance" in (result.error or "")


async def test_a_retried_turn_that_succeeds_is_not_a_failure() -> None:
    """A transient error pi recovered from is not the run's outcome.

    `turn_start` clears the pending error, so only an error still standing when
    the stream ends condemns the run -- otherwise every rate-limited-then-
    retried run would report FAILED while holding a perfectly good answer.
    """
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _failed_turn("429: rate limited")},
                {"do": "emit", "frame": {"type": "agent_end", "willRetry": True}},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _text("the answer")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "the answer"
    assert result.error is None
    assert result.turns == 2


async def test_a_successful_turn_end_carries_no_error() -> None:
    """`turn_end` fires on every run, so a normal one must stay a success."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _text("done")},
                {
                    "do": "emit",
                    "frame": {
                        "type": "turn_end",
                        "message": {"role": "assistant", "stopReason": "stop"},
                    },
                },
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "done"
    assert result.error is None


async def test_provider_error_and_a_truncated_stream_report_both() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _failed_turn("401: invalid api key")},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert "invalid api key" in (result.error or "")
    assert "without settling" in (result.error or "")


async def test_confirm_is_answered_with_confirmed_not_value(
    tmp_path: pathlib.Path,
) -> None:
    """Reply shapes differ per dialog, and getting it wrong is silent.

    pi falls back to its own default on a malformed reply exactly as it does on
    no reply, so a `confirm` answered with `value` looks like it worked.
    """
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-1",
                        "method": "confirm",
                        "title": "Proceed?",
                        "timeout": 5000,
                    },
                },
                {"do": "await_ui_response"},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ],
        },
        handler_config={"ui_policy": {"confirm": "yes"}},
    )

    assert result.status is AgentRunStatus.SETTLED
    reply = _records(record)[0]["ui_response"]
    assert reply == {"type": "extension_ui_response", "id": "ui-1", "confirmed": True}


async def test_select_is_answered_with_value(tmp_path: pathlib.Path) -> None:
    record = tmp_path / "record.jsonl"
    await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-2",
                        "method": "select",
                        "title": "Allow?",
                        "options": ["Allow", "Block"],
                    },
                },
                {"do": "await_ui_response"},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ],
        },
        handler_config={"ui_policy": {"select": "Block"}},
    )

    reply = _records(record)[0]["ui_response"]
    assert reply == {"type": "extension_ui_response", "id": "ui-2", "value": "Block"}


async def test_default_policy_refuses_and_aborts(tmp_path: pathlib.Path) -> None:
    """No configured policy means no answer is available, so the run stops.

    `REFUSED_INTERACTION` rather than `FAILED`: nothing went wrong, the run
    needed a human. A CI caller has to be able to tell those apart.
    """
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-3",
                        "method": "input",
                        "title": "Which branch?",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        }
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert "Which branch?" in (result.error or "")

    records = _records(record)
    assert records[0]["ui_response"]["cancelled"] is True
    assert records[1] == {"aborted": True}


async def test_unknown_ui_method_fails_closed(tmp_path: pathlib.Path) -> None:
    """A dialog this integration does not know must not be met with silence.

    Silence is not neutral: pi resolves an unanswered request with its own
    default once the timeout expires. Treating "not a method I reply to" as
    "fire and forget" would route every future pi dialog into that path.
    """
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-4",
                        "method": "wizard",
                        "title": "Pick a plan",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        }
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert "wizard" in (result.error or "")
    assert _records(record)[0]["ui_response"]["cancelled"] is True


async def test_fire_and_forget_notice_is_not_answered() -> None:
    """`notify` expects no reply; sending one would be an unmatched id."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-5",
                        "method": "notify",
                        "message": "heads up",
                        "notifyType": "warning",
                    },
                },
                {"do": "emit", "frame": _text("carried on")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "carried on"


async def test_stream_ending_without_settling_is_a_failure() -> None:
    """EOF is not completion. Reporting SETTLED here would tell the caller a
    truncated run finished its work."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("half an ans")},
                {"do": "exit", "code": 3},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "half an ans"
    assert "without settling" in (result.error or "")
    assert "code 3" in (result.error or "")


async def test_non_json_line_is_skipped_not_fatal() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit_raw", "text": "pi v1.2.3 starting\n"},
                {"do": "emit", "frame": _text("still fine")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "still fine"


async def test_a_wedged_agent_is_aborted_at_the_settle_timeout(
    tmp_path: pathlib.Path,
) -> None:
    """An agent loop has no natural bound, so an unbounded run would hold an ER
    subprocess slot indefinitely."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("thinking")},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        },
        handler_config={"settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not settle" in (result.error or "")
    assert _records(record) == [{"aborted": True}]


def _assistant_message(
    usage: dict[str, Any] | None,
    *,
    event: str = "message_end",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [],
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "stopReason": "stop",
    }
    if usage is not None:
        message["usage"] = usage
    return {"type": event, "message": message}


def _usage(input_tokens: int, output_tokens: int, cost: float = 0.0) -> dict[str, Any]:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": input_tokens + output_tokens,
        "cost": {
            "input": cost,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost,
        },
    }


_STATS_DATA = {
    "sessionId": "abc123",
    "tokens": {
        "input": 5000,
        "output": 900,
        "cacheRead": 4000,
        "cacheWrite": 100,
        "total": 10000,
    },
    "cost": 0.0125,
}


async def test_session_stats_supersede_the_summed_message_usage() -> None:
    """pi's own totals also cover what its tools and its compaction spent.

    Summing the message frames sees only the assistant turns, so a run whose
    agent did most of its token spending inside tool calls would report a
    fraction of what it actually cost.
    """
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "turn_start"}},
                {"do": "emit", "frame": _assistant_message(_usage(100, 20, 0.001))},
                {"do": "emit", "frame": _text("done")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "answer_session_stats", "data": _STATS_DATA},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.usage is not None
    assert result.usage.total_tokens == 10000
    assert result.usage.input_tokens == 5000
    assert result.usage.cache_read_tokens == 4000
    assert result.usage.approx_cost_usd == 0.0125
    # The stats response names no backend, so the identity is carried over from
    # the message frames rather than lost with the summed figures.
    assert result.usage.provider == "deepseek"
    assert result.usage.model == "deepseek-v4-flash"


async def test_summed_message_usage_is_the_fallback_when_stats_never_arrive() -> None:
    """pi has to be alive to answer, and the paths that need usage most are the
    ones where it is not."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _assistant_message(_usage(100, 20, 0.001))},
                {"do": "emit", "frame": _assistant_message(_usage(300, 40, 0.003))},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 400
    assert result.usage.output_tokens == 60
    assert result.usage.total_tokens == 460
    assert result.usage.approx_cost_usd == pytest.approx(0.004)


async def test_turn_end_does_not_double_count_the_message_it_repeats() -> None:
    """`message_end` and `turn_end` carry the same message. Accumulating from
    both doubles every figure, the reported bill included."""
    usage = _usage(100, 20, 0.001)
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _assistant_message(usage)},
                {"do": "emit", "frame": _assistant_message(usage, event="turn_end")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.usage is not None
    assert result.usage.total_tokens == 120


async def test_usage_is_reported_on_a_failed_run() -> None:
    """A run that spent real money and then failed is exactly when the number
    is worth having."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _assistant_message(_usage(100, 20, 0.001))},
                {"do": "emit", "frame": _failed_turn("402: Insufficient Balance")},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.usage is not None
    assert result.usage.total_tokens == 120


async def test_tokens_at_zero_cost_report_an_unknown_price_not_a_free_run() -> None:
    """pi always emits a cost, so an unpriced model and a free one look
    identical on the wire. Reporting `$0.0000` for a model pi has no price for
    is a confident wrong answer; unknown is the true one."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _assistant_message(_usage(100, 20))},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.usage is not None
    assert result.usage.total_tokens == 120
    assert result.usage.approx_cost_usd is None


async def test_a_backend_reporting_no_usage_leaves_it_unset() -> None:
    """`None` rather than a zeroed `AgentRunUsage`: nothing was reported, which
    is not the same as a run that reported nothing was spent."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("done")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.usage is None
    # Measured by the handler, so it survives a backend that reports nothing.
    assert result.duration_sec is not None


async def test_a_rejected_stats_request_falls_back_rather_than_failing() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _assistant_message(_usage(100, 20, 0.001))},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "answer_session_stats", "success": False, "data": None},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.usage is not None
    assert result.usage.total_tokens == 120


def test_build_command_carries_model_and_provider() -> None:
    """The real command builder, which the fake-pi tests deliberately replace."""
    handler = PiAgentHandler.__new__(PiAgentHandler)
    settings = pi_agent_handler._PiRunSettings(
        model="deepseek-v4-flash", provider="deepseek", settle_timeout_sec=900.0
    )

    command = PiAgentHandler._build_command(handler, settings)

    assert command == [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--model",
        "deepseek-v4-flash",
        "--provider",
        "deepseek",
    ]


@pytest.mark.parametrize("field", ["model", "provider"])
def test_build_command_omits_unset_options(field: str) -> None:
    handler = PiAgentHandler.__new__(PiAgentHandler)
    values = {"model": "m", "provider": "p"} | {field: None}
    settings = pi_agent_handler._PiRunSettings(settle_timeout_sec=900.0, **values)

    assert f"--{field}" not in PiAgentHandler._build_command(handler, settings)


async def test_default_config_records_the_mode_flags_only(
    tmp_path: pathlib.Path,
) -> None:
    """A run with no profile and no top-level model carries neither flag, so a
    later test that sees a flag knows where it came from."""
    record = tmp_path / "record.jsonl"
    await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ],
        }
    )

    argv = next(entry["argv"] for entry in _records(record) if "argv" in entry)
    assert argv == ["--mode", "rpc", "--no-session"]


def _recorded_argv(record: pathlib.Path) -> list[str]:
    return next(entry["argv"] for entry in _records(record) if "argv" in entry)


def _flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _settled_scenario(record: pathlib.Path) -> dict[str, Any]:
    return {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "await_prompt"},
            {"do": "emit", "frame": {"type": "agent_settled"}},
        ],
    }


async def test_profile_selects_its_model_and_provider(tmp_path: pathlib.Path) -> None:
    """A profile is the only way one task runs on a different model than
    another, so its fields must reach the command rather than living in config
    that is merely parsed."""
    record = tmp_path / "record.jsonl"
    await _run(
        _settled_scenario(record),
        handler_config={
            "model": "M1",
            "provider": "P1",
            "profiles": {"p": {"model": "M2", "provider": "P2"}},
        },
        profile="p",
    )

    argv = _recorded_argv(record)
    assert _flag_value(argv, "--model") == "M2"
    assert _flag_value(argv, "--provider") == "P2"


async def test_no_profile_uses_the_top_level_values(tmp_path: pathlib.Path) -> None:
    """An existing caller that sends no profile must get exactly today's command."""
    record = tmp_path / "record.jsonl"
    await _run(
        _settled_scenario(record),
        handler_config={
            "model": "M1",
            "provider": "P1",
            "profiles": {"p": {"model": "M2", "provider": "P2"}},
        },
    )

    argv = _recorded_argv(record)
    assert _flag_value(argv, "--model") == "M1"
    assert _flag_value(argv, "--provider") == "P1"


class _RecordingCommandRunner:
    def __init__(self) -> None:
        self.run_calls = 0

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        self.run_calls += 1
        raise AssertionError("an unknown profile must not spawn a process")


class _RaisingCommandRunner:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        raise self._error

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        raise NotImplementedError


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        icommandrunner.UnsafeBatchArgumentError(
            "argument 1 cannot be passed safely to batch file pi.cmd"
        ),
        icommandrunner.UnlaunchableProgramError(
            "pi resolves to pi.js, which CreateProcess cannot launch"
        ),
    ],
)
async def test_a_run_that_cannot_spawn_pi_is_a_structured_failure(
    error: Exception,
) -> None:
    """An unstartable pi -- missing from PATH, or a Windows shim refusing an
    argument -- is a structured `FAILED` result naming the program and the
    spawner's reason, never an exception out of the action."""
    result = await _run(
        {"steps": []},
        service_overrides={icommandrunner.ICommandRunner: _RaisingCommandRunner(error)},
    )

    assert result.status is AgentRunStatus.FAILED
    assert sys.executable in result.error
    assert getattr(error, "strerror", None) or str(error) in result.error
    assert result.duration_sec == 0.0


async def test_unknown_profile_fails_before_spawning() -> None:
    """A typo in a profile name must fail with a message that names the profile
    and the ones that exist, and must not pay for a model run to discover it."""
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        handler_config={"profiles": {"b": {}, "a": {}}},
        profile="nope",
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "unknown agent profile 'nope'; configured profiles: a, b"
    assert runner.run_calls == 0


async def test_profile_settle_timeout_overrides_the_top_level(
    tmp_path: pathlib.Path,
) -> None:
    """A long top-level timeout must not keep a profile that asked for a short
    one waiting: the profile decides how long its own run may take."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("thinking")},
                {"do": "sleep", "seconds": 300},
            ],
        },
        handler_config={
            "settle_timeout_sec": 900,
            "profiles": {"p": {"settle_timeout_sec": 0.5}},
        },
        profile="p",
    )

    assert result.status is AgentRunStatus.FAILED
    assert "0.5s" in (result.error or "")


async def test_confirm_policy_no_denies_rather_than_approving(
    tmp_path: pathlib.Path,
) -> None:
    """`confirm` carries a boolean, and truthiness is the wrong reader of it:
    `bool("no")` is `True`, so coercing the policy string would make a configured
    refusal approve whatever pi asked to do -- an approval gate that inverts."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-5",
                        "method": "confirm",
                        "title": "Delete the branch?",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ],
        },
        handler_config={"ui_policy": {"confirm": "no"}},
    )

    assert result.status is AgentRunStatus.SETTLED
    reply = _records(record)[0]["ui_response"]
    assert reply == {"type": "extension_ui_response", "id": "ui-5", "confirmed": False}


async def test_confirm_policy_that_is_not_a_yes_or_no_fails_closed(
    tmp_path: pathlib.Path,
) -> None:
    """A confirm dialog has no free-text answer to fall through to, so a policy
    that is neither yes nor no is a misconfiguration -- refused and reported,
    never resolved by guessing a side."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-6",
                        "method": "confirm",
                        "title": "Push to main?",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        },
        handler_config={"ui_policy": {"confirm": "maybe"}},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert "maybe" in (result.error or "")
    assert _records(record)[0]["ui_response"]["cancelled"] is True


async def test_ui_request_without_a_method_is_declined_not_ignored(
    tmp_path: pathlib.Path,
) -> None:
    """A frame missing `method` is malformed, not harmless. Falling silent lets
    pi's own timeout resolve it with its default -- the same silent auto-answer
    an unknown method is deliberately refused for. As long as the id is there, a
    decline can be addressed, so it is sent."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-7",
                        "title": "Something?",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        }
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    reply = _records(record)[0]["ui_response"]
    assert reply == {"type": "extension_ui_response", "id": "ui-7", "cancelled": True}


def _is_gone(pid: int) -> bool:
    """Whether `pid` is neither alive nor a zombie this process still owns."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The pid was recycled by a process we do not own -- still not ours.
        return True
    return False


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _is_gone(pid)


async def test_a_wedged_run_is_torn_down_with_the_tree_it_spawned(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`abort` is a request, and a pi that ignores it must still be stopped.

    Otherwise the run that timed out keeps write access to the project, keeps
    spending, and keeps the subprocess slot the timeout exists to release. This
    one ignores SIGTERM and has a child of its own, so both the last rung of the
    ladder and the process group have to work.
    """
    monkeypatch.setattr(pi_agent_handler, "_ABORT_GRACE_SEC", 0.3)
    monkeypatch.setattr(pi_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)

    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "await_prompt"},
                {"do": "ignore_sigterm"},
                {"do": "spawn_child"},
                {"do": "sleep", "seconds": 300},
            ],
        },
        handler_config={"settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not settle within" in (result.error or "")

    pids = next(entry["pids"] for entry in _records(record) if "pids" in entry)
    assert await _wait_gone(pids["agent"]), "pi outlived its own timeout"
    assert await _wait_gone(pids["child"]), "pi's child was left running"


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
}


def _fenced(payload: str) -> str:
    return f"```json\n{payload}\n```"


async def test_output_schema_appends_the_instruction_to_the_prompt(
    tmp_path: pathlib.Path,
) -> None:
    """The prompt is the only channel pi has for the schema, so the instruction
    must reach pi byte for byte and carry the schema the caller asked for."""
    record = tmp_path / "record.jsonl"
    await _run(
        _settled_scenario(record),
        prompt="do the thing",
        output_schema=_SCHEMA,
    )

    prompt = next(entry["prompt"] for entry in _records(record) if "prompt" in entry)
    assert prompt == "do the thing" + backend_support.json_output_instruction(_SCHEMA)
    assert json.dumps(_SCHEMA, indent=2) in prompt


async def test_no_schema_sends_the_prompt_unchanged(tmp_path: pathlib.Path) -> None:
    """A caller that asked for no structured output must send exactly the text
    it wrote, with no instruction appended."""
    record = tmp_path / "record.jsonl"
    result = await _run(_settled_scenario(record), prompt="do the thing")

    prompt = next(entry["prompt"] for entry in _records(record) if "prompt" in entry)
    assert prompt == "do the thing"
    assert result.structured_output is None


async def test_the_last_fenced_block_is_the_structured_output() -> None:
    """A model that shows intermediate JSON and then an answer puts the answer
    last; reading the first block would return its own workings."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("first\n" + _fenced('{"n": 1}') + "\n")},
                {"do": "emit", "frame": _text("then\n" + _fenced('{"n": 2}') + "\n")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        },
        output_schema=_SCHEMA,
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"n": 2}


async def test_missing_block_fails_and_keeps_the_raw_output() -> None:
    """A settled run with no block is a failure with a named reason, and the
    raw text stays available to whoever has to debug it."""
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("no json here")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        },
        output_schema=_SCHEMA,
    )

    assert result.status is AgentRunStatus.FAILED
    assert (result.error or "").startswith("no fenced json block")
    assert result.output == "no json here"


async def test_invalid_block_fails_and_keeps_the_raw_output() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text(_fenced("{oops}"))},
                {"do": "emit", "frame": {"type": "agent_settled"}},
            ]
        },
        output_schema=_SCHEMA,
    )

    assert result.status is AgentRunStatus.FAILED
    assert (result.error or "").startswith("invalid JSON in the final json block")
    assert result.output == _fenced("{oops}")
