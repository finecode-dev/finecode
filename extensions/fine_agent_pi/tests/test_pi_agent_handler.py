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
import dataclasses
import json
import os
import pathlib
import sys
import time
import typing
import uuid
from typing import Any

import pytest
from fine_agent import backend_support
from fine_agent.history_types import (
    AgentBackend,
    HistoryDiscoveryStatus,
    NativeHistoryFormat,
    NativeHistoryRole,
    RelatedHistoryStatus,
    UsageScope,
    UsageSource,
)
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    AgentSessionRef,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner import partial_result_sender
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session, run_handler

from fine_agent_pi import pi_agent_handler, pi_history, pi_rpc
from fine_agent_pi.pi_agent_handler import PiAgentHandler
from fine_agent_pi.run_pi_agent_task_action import RunPiAgentTaskAction

_FAKE_PI = pathlib.Path(__file__).parent / "fake_pi.py"


_ACTIVE_SCENARIO: dict[str, Any] = {}


def _discard_partial(
    _token: int | str,
    _result: code_action.RunActionResult,
    _formats: list[str] | None,
) -> None:
    return None


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


class _PiFakeScenario(typing.TypedDict, total=False):
    """New fake-pi control fields, beyond the established step protocol.

    `wrong_id` overrides the session id in a deliberately wrong `get_state`
    answer. Unlisted step dicts keep their existing ad-hoc shape.
    """

    steps: list[dict[str, Any]]
    wrong_id: str
    preserve_stats_ids: bool


@pytest.fixture(autouse=True)
def _isolated_pi_sessions(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native sessions stay inside the test's tmp dir, never the dev home."""
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi-sessions"))


async def _run(
    scenario: dict[str, Any],
    *,
    prompt: str = "do the thing",
    handler_config: dict[str, Any] | None = None,
    profile: str | None = None,
    output_schema: dict[str, Any] | None = None,
    service_overrides: dict[Any, Any] | None = None,
    capture_history: bool = False,
    continue_session: AgentSessionRef | None = None,
    project_dir: pathlib.Path | None = None,
    action_cls: Any = RunAgentTaskAction,
) -> RunAgentTaskRunResult:
    global _ACTIVE_SCENARIO
    _ACTIVE_SCENARIO = scenario
    try:
        result = await run_handler(
            _FakePiHandler,
            RunAgentTaskRunPayload(
                prompt=prompt,
                profile=profile,
                output_schema=output_schema,
                capture_history=capture_history,
                continue_session=continue_session,
            ),
            action_cls=action_cls,
            handler_config=handler_config,
            service_overrides=service_overrides,
            project_dir=project_dir,
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


async def test_snapshot_sends_finish_with_full_result_and_token_run_keeps_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        run_action_service,
        "partial_result_sender",
        partial_result_sender.PartialResultSender(
            sender=_discard_partial,
            wait_time_ms=300,
        ),
    )
    action_source = f"{RunAgentTaskAction.__module__}.{RunAgentTaskAction.__qualname__}"
    handler_source = f"{_FakePiHandler.__module__}.{_FakePiHandler.__qualname__}"
    actions = {
        RunAgentTaskAction.__name__: {
            "source": action_source,
            "handlers": [{"name": "pi", "source": handler_source}],
        }
    }
    scenario = {
        "steps": [
            {"do": "await_prompt"},
            {
                "do": "emit",
                "frame": {"type": "message_end", "message": {"role": "user"}},
            },
            {"do": "emit", "frame": _text("hello")},
            {"do": "emit", "frame": _assistant_message(_usage(1, 2))},
            {"do": "emit", "frame": {"type": "agent_settled"}},
        ]
    }
    _ACTIVE_SCENARIO.clear()
    _ACTIVE_SCENARIO.update(scenario)
    try:
        async with handler_test_session(
            project_dir=tmp_path,
            actions=actions,
        ) as session:
            token_result = await session.run_action(
                RunAgentTaskAction.__name__,
                RunAgentTaskRunPayload(prompt="test"),
                partial_result_token="token",
            )
            token_events = [
                typing.cast(RunAgentTaskRunResult, item)
                for item in session.partial_results.events
            ]

            _ACTIVE_SCENARIO.clear()
            _ACTIVE_SCENARIO.update(scenario)
            plain_result = await session.run_action(
                RunAgentTaskAction.__name__, RunAgentTaskRunPayload(prompt="test")
            )
    finally:
        _ACTIVE_SCENARIO.clear()

    assert isinstance(token_result, RunAgentTaskRunResult)
    assert isinstance(plain_result, RunAgentTaskRunResult)
    assert dataclasses.replace(token_result, duration_sec=None, session=None) == (
        dataclasses.replace(plain_result, duration_sec=None, session=None)
    )
    assert token_result.output == "hello"
    assert token_events[-1] == token_result
    assert token_events[-1].usage is not None
    snapshots = token_events[:-1]
    assert snapshots
    assert all(item.status is AgentRunStatus.RUNNING for item in snapshots)
    assert all(item.usage is None and item.session is None for item in snapshots)
    assert [item.output for item in snapshots] == ["hello"]


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
        model="deepseek-v4-flash",
        provider="deepseek",
        thinking="high",
        settle_timeout_sec=900.0,
    )

    session_id = "11111111-2222-4333-8444-555555555555"
    command = PiAgentHandler._build_command(handler, settings, session_id)

    assert command == [
        "pi",
        "--mode",
        "rpc",
        "--session-id",
        session_id,
        "--model",
        "deepseek-v4-flash",
        "--provider",
        "deepseek",
        "--thinking",
        "high",
    ]


@pytest.mark.parametrize("field", ["model", "provider", "thinking"])
def test_build_command_omits_unset_options(field: str) -> None:
    handler = PiAgentHandler.__new__(PiAgentHandler)
    values = {"model": "m", "provider": "p", "thinking": "t"} | {field: None}
    settings = pi_agent_handler._PiRunSettings(settle_timeout_sec=900.0, **values)

    command = PiAgentHandler._build_command(
        handler, settings, "11111111-2222-4333-8444-555555555555"
    )
    assert f"--{field}" not in command


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
    assert argv[:3] == ["--mode", "rpc", "--session-id"]
    assert uuid.UUID(argv[3])
    assert "--no-session" not in argv
    assert "--session" not in argv
    assert "--model" not in argv
    assert "--provider" not in argv


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


@pytest.mark.parametrize("action_cls", [RunAgentTaskAction, RunPiAgentTaskAction])
async def test_profile_selects_its_model_and_provider(
    tmp_path: pathlib.Path, action_cls: Any
) -> None:
    """A profile is the only way one task runs on a different model than
    another, so its fields must reach the command rather than living in config
    that is merely parsed."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        _settled_scenario(record),
        handler_config={
            "model": "M1",
            "provider": "P1",
            "profiles": {"p": {"model": "M2", "provider": "P2"}},
        },
        profile="p",
        action_cls=action_cls,
    )

    assert isinstance(result, RunAgentTaskRunResult)
    assert result.status is AgentRunStatus.SETTLED

    argv = _recorded_argv(record)
    assert _flag_value(argv, "--model") == "M2"
    assert _flag_value(argv, "--provider") == "P2"


async def test_profile_thinking_overrides_and_unset_inherits(
    tmp_path: pathlib.Path,
) -> None:
    config = {
        "model": "M1",
        "thinking": "low",
        "profiles": {"set": {"thinking": "high"}, "inherit": {"model": "M2"}},
    }
    for profile, expected in (("set", "high"), ("inherit", "low"), (None, "low")):
        record = tmp_path / f"record-{profile}.jsonl"
        await _run(_settled_scenario(record), handler_config=config, profile=profile)
        assert _flag_value(_recorded_argv(record), "--thinking") == expected


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
        self.start_long_running_calls = 0

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.run_calls += 1
        raise AssertionError("agent processes must start through start_long_running")

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self.run(
            cmd, cwd=cwd, env=env, new_process_group=new_process_group
        )

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        self.start_long_running_calls += 1
        raise AssertionError("an unknown profile must not spawn a process")


class _RaisingCommandRunner:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.run_calls = 0
        self.start_long_running_calls = 0

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.run_calls += 1
        raise AssertionError("agent processes must start through start_long_running")

    async def run_to_completion(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = True,
        input: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self.run(
            cmd, cwd=cwd, env=env, new_process_group=new_process_group
        )

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        self.start_long_running_calls += 1
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
        icommandrunner.ProgramStartError(
            "pi", None, FileNotFoundError(2, "No such file or directory")
        ),
        icommandrunner.ProgramStartError(
            "pi", None, PermissionError(13, "Permission denied")
        ),
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


async def test_agent_spawns_through_start_long_running_not_run() -> None:
    """The agent run is long-lived, so it must not hold a bounded-job slot."""
    runner = _RaisingCommandRunner(
        icommandrunner.ProgramStartError(
            "pi", None, FileNotFoundError(2, "No such file or directory")
        )
    )
    result = await _run(
        {"steps": []},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert runner.start_long_running_calls == 1
    assert runner.run_calls == 0


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


@pytest.mark.parametrize("action_cls", [RunAgentTaskAction, RunPiAgentTaskAction])
async def test_the_last_fenced_block_is_the_structured_output(
    action_cls: Any,
) -> None:
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
        action_cls=action_cls,
    )

    assert isinstance(result, RunAgentTaskRunResult)

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


def _captured_scenario(session_dir: pathlib.Path) -> dict[str, Any]:
    return {
        "session_dir": str(session_dir),
        "record_driver": True,
        "native_entries": [
            {
                "type": "message",
                "id": "assistant-1",
                "message": {"role": "assistant", "content": "assistant marker ALPHA"},
            },
            {
                "type": "message",
                "id": "tool-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "read",
                            "input": "tool marker BETA",
                        }
                    ],
                },
            },
        ],
        "steps": [
            {"do": "await_prompt"},
            {"do": "emit", "frame": {"type": "turn_start"}},
            {"do": "emit", "frame": _text("hello")},
            {"do": "emit", "frame": {"type": "agent_settled"}},
            {"do": "answer_session_stats", "data": dict(_STATS_DATA)},
        ],
    }


async def test_uncaptured_has_no_observation() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("hi")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "exit", "code": 0},
            ]
        }
    )

    assert result.observation is None


async def test_captured_run_reports_available_history(tmp_path: pathlib.Path) -> None:
    record = tmp_path / "record.jsonl"
    scenario = _captured_scenario(tmp_path)
    scenario["record_path"] = str(record)
    result = await _run(scenario, capture_history=True)

    assert result.status is AgentRunStatus.SETTLED
    observation = result.observation
    assert observation is not None
    assert observation.backend is AgentBackend.PI
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert observation.session_id is not None
    assert len(observation.files) == 1
    main = observation.files[0]
    assert main.role is NativeHistoryRole.MAIN
    assert main.format is NativeHistoryFormat.PI
    assert main.native_session_id == observation.session_id
    assert observation.related_status is RelatedHistoryStatus.UNAVAILABLE
    assert observation.usage_provenance.token_source is UsageSource.PI_STATS
    assert observation.usage_provenance.token_scope is UsageScope.SESSION
    assert observation.backend_version == "1.3.0"
    argv = next(entry["argv"] for entry in _records(record) if "argv" in entry)
    assert "--no-session" not in argv
    assert "--session" not in argv
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == observation.session_id
    source_path = resource_uri_to_path(main.source_uri)
    assert source_path.is_file()
    content = source_path.read_text(encoding="utf-8")
    assert observation.session_id in content
    assert "do the thing" in content
    assert "assistant marker ALPHA" in content
    assert "tool marker BETA" in content


async def test_captured_decoy_with_wrong_session_is_ignored(
    tmp_path: pathlib.Path,
) -> None:
    decoy = tmp_path / "2026-01-02T00-00-00_wrong.jsonl"
    _ = decoy.write_text(
        json.dumps({"type": "session", "version": 3, "id": "wrong-id"}) + "\n",
        encoding="utf-8",
    )
    scenario = _captured_scenario(tmp_path)
    result = await _run(scenario, capture_history=True)

    assert result.observation is not None
    assert result.observation.session_id != "wrong-id"
    assert result.observation.files[0].native_session_id != "wrong-id"


async def test_captured_stats_fallback_keeps_message_usage(
    tmp_path: pathlib.Path,
) -> None:
    scenario = _captured_scenario(tmp_path)
    scenario["steps"] = [
        {"do": "await_prompt"},
        {"do": "emit", "frame": _assistant_message(_usage(100, 20, 0.001))},
        {"do": "emit", "frame": {"type": "agent_settled"}},
        {"do": "exit", "code": 0},
    ]
    result = await _run(scenario, capture_history=True)

    assert result.usage is not None
    assert result.usage.total_tokens == 120
    assert result.observation is not None
    # The stats never arrived, but the startup identity check still named the
    # native session, so history is located from that file independently of
    # the missing totals.
    assert result.observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert result.observation.session_id is not None
    assert result.observation.usage_provenance.token_source is UsageSource.PI_MESSAGES
    assert result.observation.usage_provenance.token_scope is UsageScope.MESSAGE_SUM


async def test_captured_unknown_profile_has_presession_observation() -> None:
    result = await _run(
        {"steps": []},
        profile="nope",
        handler_config={"profiles": {"a": {}}},
        capture_history=True,
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.observation is not None
    assert result.observation.files == []
    assert result.observation.session_id is None


async def test_captured_version_failure_leaves_gap_but_keeps_history(
    tmp_path: pathlib.Path,
) -> None:
    scenario = _captured_scenario(tmp_path)
    scenario["version"] = ""
    result = await _run(scenario, capture_history=True)

    assert result.status is AgentRunStatus.SETTLED
    assert result.observation is not None
    assert result.observation.backend_version is None
    assert any("version" in gap for gap in result.observation.gaps)


def _project_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)
    return project_dir


def _session_ref(
    project_dir: pathlib.Path,
    session_id: str = "11111111-2222-4333-8444-555555555555",
    backend: AgentBackend = AgentBackend.PI,
) -> AgentSessionRef:
    return AgentSessionRef(
        backend=backend,
        session_id=session_id,
        project_dir=path_to_resource_uri(project_dir),
    )


def _native_files(session_dir: pathlib.Path) -> set[str]:
    if not session_dir.is_dir():
        return set()
    return {
        str(path.relative_to(session_dir))
        for path in session_dir.rglob("*.jsonl")
        if path.is_file()
    }


def _main_session_file(session_dir: pathlib.Path, session_id: str) -> pathlib.Path:
    """The one session file whose header id is *session_id*."""
    matches = [
        path
        for path in session_dir.rglob("*.jsonl")
        if path.is_file()
        and json.loads(path.read_text(encoding="utf-8").splitlines()[0]).get("id")
        == session_id
    ]
    assert len(matches) == 1
    return matches[0]


def _new_run_scenario(record: pathlib.Path) -> dict[str, Any]:
    return {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "await_prompt"},
            {"do": "emit", "frame": {"type": "turn_start"}},
            {"do": "emit", "frame": _text("first answer")},
            {"do": "emit", "frame": {"type": "agent_settled"}},
            {"do": "answer_session_stats", "data": dict(_STATS_DATA)},
        ],
    }


def _resume_run_scenario(record: pathlib.Path) -> dict[str, Any]:
    return {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "await_get_state"},
            {"do": "answer_get_state"},
            {"do": "await_prompt"},
            {"do": "emit", "frame": {"type": "turn_start"}},
            {"do": "emit", "frame": _text("resumed answer")},
            {"do": "emit", "frame": {"type": "agent_settled"}},
            {"do": "answer_session_stats", "data": dict(_STATS_DATA)},
        ],
    }


@pytest.mark.parametrize("capture_history", [False, True])
async def test_new_run_creates_session(
    tmp_path: pathlib.Path, capture_history: bool
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    result = await _run(
        _new_run_scenario(tmp_path / "record.jsonl"),
        project_dir=project_dir,
        capture_history=capture_history,
    )

    assert result.status is AgentRunStatus.SETTLED, result.error
    assert result.session is not None
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    main_file = _main_session_file(session_dir, result.session.session_id)
    assert "do the thing" in main_file.read_text(encoding="utf-8")
    assert (result.observation is not None) is capture_history


async def test_settled_run_reports_session_and_continuations_reuse_it(
    tmp_path: pathlib.Path,
) -> None:
    """A new settled run hands out its native session; later prompts resume it.

    The second prompt must reopen the same native session rather than start
    one, and a third can reuse the reference the second run returned. The
    session-file set does not grow; the decoy is untouched.
    """
    project_dir = _project_dir(tmp_path)
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    decoy = session_dir / "2026-01-02T00-00-00_wrong.jsonl"
    first_record = tmp_path / "first-record.jsonl"
    first = await _run(
        _new_run_scenario(first_record),
        prompt="first",
        project_dir=project_dir,
        handler_config={"model": "m"},
    )

    assert first.status is AgentRunStatus.SETTLED
    assert first.session is not None
    assert first.session.backend is AgentBackend.PI
    assert uuid.UUID(first.session.session_id)
    assert first.session.project_dir == path_to_resource_uri(project_dir)
    main_file = _main_session_file(session_dir, first.session.session_id)
    assert "first" in main_file.read_text(encoding="utf-8")
    files_before = _native_files(session_dir)
    assert files_before
    _ = decoy.write_text(
        json.dumps({"type": "session", "version": 3, "id": "wrong-id"}) + "\n",
        encoding="utf-8",
    )
    decoy_bytes = decoy.read_bytes()

    second_record = tmp_path / "second-record.jsonl"
    second = await _run(
        _resume_run_scenario(second_record),
        prompt="second",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    resumed = main_file.read_text(encoding="utf-8")
    assert "first" in resumed and "second" in resumed
    assert _native_files(session_dir) == files_before | {decoy.name}
    assert decoy.read_bytes() == decoy_bytes
    argv = next(entry["argv"] for entry in _records(second_record) if "argv" in entry)
    assert "--session" in argv
    assert argv[argv.index("--session") + 1] == first.session.session_id
    assert "--session-id" not in argv
    assert "--no-session" not in argv

    third_record = tmp_path / "third-record.jsonl"
    third = await _run(
        _resume_run_scenario(third_record),
        prompt="third",
        project_dir=project_dir,
        continue_session=second.session,
        handler_config={"model": "m"},
    )

    assert third.status is AgentRunStatus.SETTLED
    assert third.session == first.session
    assert "third" in main_file.read_text(encoding="utf-8")
    assert _native_files(session_dir) == files_before | {decoy.name}


async def test_uncaptured_continuation_still_reports_session(
    tmp_path: pathlib.Path,
) -> None:
    """Capture off returns no observation but still reports the session."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None

    second = await _run(
        _resume_run_scenario(tmp_path / "second-record.jsonl"),
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    assert second.observation is None


async def test_continuation_argv_carries_current_model_and_provider(
    tmp_path: pathlib.Path,
) -> None:
    """The resume argv carries the current settings, not saved ones."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "old-model", "provider": "old-provider"},
    )
    assert first.session is not None
    record = tmp_path / "resume-record.jsonl"
    second = await _run(
        _resume_run_scenario(record),
        prompt="again",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "new-model", "provider": "new-provider"},
    )

    assert second.status is AgentRunStatus.SETTLED
    argv = next(entry["argv"] for entry in _records(record) if "argv" in entry)
    assert argv[:4] == ["--mode", "rpc", "--session", first.session.session_id]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "new-model"
    assert "--provider" in argv and argv[argv.index("--provider") + 1] == "new-provider"
    prompts = [entry["prompt"] for entry in _records(record) if "prompt" in entry]
    assert prompts == ["again"]


async def test_continuation_rejects_non_uuid_ids_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    """Names and fragments fail before any backend, project or lookup work."""
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    for bad_id in ("", "thread-1", "abc123", "{11111111-2222-4333-8444-555555555555}"):
        result = await _run(
            {"steps": []},
            project_dir=project_dir,
            continue_session=_session_ref(project_dir, session_id=bad_id),
            handler_config={"model": "m"},
            service_overrides={icommandrunner.ICommandRunner: runner},
        )
        assert result.status is AgentRunStatus.FAILED
        assert "session" in (result.error or "").lower()
    assert runner.start_long_running_calls == 0


async def test_continuation_rejects_backend_mismatch_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=_session_ref(project_dir, backend=AgentBackend.CODEX),
        handler_config={"model": "m"},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "codex" in (result.error or "")
    assert runner.start_long_running_calls == 0


async def test_continuation_rejects_foreign_project_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    ref = AgentSessionRef(
        backend=AgentBackend.PI,
        session_id="11111111-2222-4333-8444-555555555555",
        project_dir=path_to_resource_uri(tmp_path / "elsewhere"),
    )
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=ref,
        handler_config={"model": "m"},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "project" in (result.error or "").lower()
    assert runner.start_long_running_calls == 0


async def test_continuation_unknown_profile_fails_without_lookup_or_spawn(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=_session_ref(project_dir),
        profile="nope",
        handler_config={"profiles": {"a": {}}},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "unknown agent profile" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_missing_session_beats_unset_model(
    tmp_path: pathlib.Path,
) -> None:
    """A missing session is reported, not the conflicting settings value."""
    project_dir = _project_dir(tmp_path)
    ref = _session_ref(project_dir, session_id="22222222-2222-4222-8222-222222222222")
    result = await _run(
        {"steps": []},
        prompt="hello?",
        project_dir=project_dir,
        continue_session=ref,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "no pi session file matches" in (result.error or "")
    assert "model" not in (result.error or "")
    assert result.session is None


async def test_continuation_missing_session_fails_and_creates_no_session(
    tmp_path: pathlib.Path,
) -> None:
    """The fake re-reads the native store; a miss creates nothing."""
    project_dir = _project_dir(tmp_path)
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    ref = _session_ref(project_dir, session_id="22222222-2222-4222-8222-222222222222")
    files_before = _native_files(session_dir)
    record = tmp_path / "miss-record.jsonl"
    scenario = _resume_run_scenario(record)
    result = await _run(
        scenario,
        prompt="hello?",
        project_dir=project_dir,
        continue_session=ref,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "no pi session file matches" in (result.error or "")
    assert result.session is None
    assert _native_files(session_dir) == files_before
    prompts = [entry for entry in _records(record) if "prompt" in entry]
    assert prompts == []


async def test_continuation_deleted_session_fails_without_creating_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session removed after the handler lookup still fails at resume."""
    project_dir = _project_dir(tmp_path)
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    files_before = _native_files(session_dir)

    real_find = pi_history.find_pi_session_file

    def _find_then_delete(
        root: pathlib.Path, session_id: str | None, marker: str | None
    ) -> tuple[str | None, pathlib.Path | None, list[str]]:
        found = real_find(root, session_id, marker)
        for path in root.rglob("*.jsonl"):
            if path.is_file():
                path.unlink()
        return found

    monkeypatch.setattr(pi_history, "find_pi_session_file", _find_then_delete)
    record = tmp_path / "deleted-record.jsonl"
    result = await _run(
        _resume_run_scenario(record),
        prompt="hello?",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "confirm" in (result.error or "").lower()
    assert result.session is None
    assert _native_files(session_dir) <= files_before
    prompts = [entry for entry in _records(record) if "prompt" in entry]
    assert prompts == []


async def test_continuation_rejects_wrong_startup_identity_before_prompt(
    tmp_path: pathlib.Path,
) -> None:
    """An unexpected startup identity fails before the task prompt is sent."""
    project_dir = _project_dir(tmp_path)
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    files_before = _native_files(session_dir)
    record = tmp_path / "wrong-id-record.jsonl"
    scenario = _resume_run_scenario(record)
    scenario["steps"] = [
        {"do": "await_get_state"},
        {"do": "answer_get_state_wrong"},
        {"do": "await_prompt"},
        {"do": "exit", "code": 0},
    ]
    result = await _run(
        scenario,
        prompt="hello?",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "unexpected session" in (result.error or "")
    assert result.session is None
    assert _native_files(session_dir) == files_before
    assert [entry for entry in _records(record) if "prompt" in entry] == []


async def test_continuation_without_model_fails_before_spawn(
    tmp_path: pathlib.Path,
) -> None:
    """With no explicit model the saved default cannot be reset by name."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    record = tmp_path / "unset-model-record.jsonl"
    result = await _run(
        _resume_run_scenario(record),
        project_dir=project_dir,
        continue_session=first.session,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "model" in (result.error or "")
    assert not record.exists()


async def test_continuation_conflicting_identities_fail_and_keep_output(
    tmp_path: pathlib.Path,
) -> None:
    """Stats disagreeing with the confirmed identity fail, keeping text."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    scenario = _resume_run_scenario(tmp_path / "conflict-record.jsonl")
    scenario["preserve_stats_ids"] = True
    scenario["steps"] = [
        {"do": "await_get_state"},
        {"do": "answer_get_state"},
        {"do": "await_prompt"},
        {"do": "emit", "frame": _text("kept text")},
        {"do": "emit", "frame": {"type": "agent_settled"}},
        {
            "do": "answer_session_stats",
            "data": {
                "sessionId": "33333333-3333-4333-8333-333333333333",
                "tokens": {},
            },
        },
    ]
    result = await _run(
        scenario,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "kept text"
    assert "conflicting session identities" in (result.error or "")
    assert result.session is None


async def test_new_run_settled_without_reported_id_fails_and_keeps_output(
    tmp_path: pathlib.Path,
) -> None:
    """An otherwise-settled new run with no identity fails, keeping raw output."""
    project_dir = _project_dir(tmp_path)
    record = tmp_path / "new-no-id-record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "preserve_stats_ids": True,
            "steps": [
                {"do": "await_get_state"},
                {"do": "answer_get_state_no_id"},
                {"do": "await_prompt"},
                {"do": "emit", "frame": _text("kept text")},
                {"do": "emit", "frame": {"type": "agent_settled"}},
                {"do": "answer_session_stats", "data": {"tokens": {}}},
            ],
        },
        project_dir=project_dir,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "kept text"
    assert "session" in (result.error or "").lower()
    assert result.session is None


async def test_refusal_without_identity_keeps_refusal(
    tmp_path: pathlib.Path,
) -> None:
    """A missing identity on a refused run never replaces that outcome."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "await_get_state"},
                {"do": "answer_get_state_no_id"},
                {"do": "await_prompt"},
                {
                    "do": "emit",
                    "frame": {
                        "type": "extension_ui_request",
                        "id": "ui-1",
                        "method": "confirm",
                        "title": "Push?",
                    },
                },
                {"do": "await_ui_response"},
                {"do": "await_abort"},
                {"do": "exit", "code": 0},
            ],
        },
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.session is None


async def test_continuation_failed_turn_keeps_failure_not_missing_id(
    tmp_path: pathlib.Path,
) -> None:
    """Existing failure precedence survives the identity merge."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    scenario = _resume_run_scenario(tmp_path / "fail-record.jsonl")
    scenario["steps"] = [
        {"do": "await_get_state"},
        {"do": "answer_get_state"},
        {"do": "await_prompt"},
        {"do": "emit", "frame": _failed_turn("402: Insufficient Balance")},
        {"do": "exit", "code": 0},
    ]
    result = await _run(
        scenario,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "Insufficient Balance" in (result.error or "")
    assert result.session is None


async def test_continuation_timeout_fails_and_reports_no_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that never settles fails on its timeout with no reference."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    scenario = _resume_run_scenario(tmp_path / "timeout-record.jsonl")
    scenario["steps"] = [
        {"do": "await_get_state"},
        {"do": "answer_get_state"},
        {"do": "await_prompt"},
        {"do": "sleep", "seconds": 300},
    ]
    result = await _run(
        scenario,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m", "settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not settle within" in (result.error or "")
    assert result.session is None


async def test_cancel_reaches_a_live_owned_continuation_and_reraises(
    tmp_path: pathlib.Path,
) -> None:
    """Withdrawing the request stops the agent; nothing is returned."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
    )
    assert first.session is not None
    record = tmp_path / "cancel-record.jsonl"
    scenario = _resume_run_scenario(record)
    scenario["steps"] = [
        {"do": "await_get_state"},
        {"do": "answer_get_state"},
        {"do": "await_prompt"},
        {"do": "spawn_child"},
        {"do": "sleep", "seconds": 300},
    ]
    task = asyncio.create_task(
        _run(
            scenario,
            project_dir=project_dir,
            continue_session=first.session,
            handler_config={"model": "m"},
        )
    )
    await asyncio.sleep(2.0)
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pids = next(entry["pids"] for entry in _records(record) if "pids" in entry)
    assert await _wait_gone(pids["agent"])
    assert await _wait_gone(pids["child"])


async def test_captured_continuation_locates_the_same_main_session(
    tmp_path: pathlib.Path,
) -> None:
    """Capture on a continuation locates the same main session file."""
    project_dir = _project_dir(tmp_path)
    session_dir = pathlib.Path(os.environ["PI_CODING_AGENT_SESSION_DIR"])
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
        capture_history=True,
    )
    assert first.session is not None
    assert first.observation is not None
    main_before = first.observation.files[0].source_uri
    files_before = _native_files(session_dir)
    second = await _run(
        _resume_run_scenario(tmp_path / "second-record.jsonl"),
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
        capture_history=True,
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    assert second.observation is not None
    assert second.observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert second.observation.session_id == first.session.session_id
    assert second.observation.files[0].source_uri == main_before
    assert _native_files(session_dir) == files_before


async def test_observation_failure_keeps_reported_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observation failure never erases an available reference."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _new_run_scenario(tmp_path / "first-record.jsonl"),
        project_dir=project_dir,
        handler_config={"model": "m"},
        capture_history=True,
    )
    assert first.session is not None

    def _fail_locate(state: Any, root: pathlib.Path) -> Any:
        raise OSError("disk is gone")

    monkeypatch.setattr(pi_history, "locate_pi_history", _fail_locate)
    second = await _run(
        _resume_run_scenario(tmp_path / "second-record.jsonl"),
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
        capture_history=True,
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    assert second.observation is not None
    assert second.observation.history_status is HistoryDiscoveryStatus.FAILED
    assert second.observation.session_id == first.session.session_id
