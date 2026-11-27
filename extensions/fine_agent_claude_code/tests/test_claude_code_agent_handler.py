"""`ClaudeCodeAgentHandler` driving a fake `claude` over the real wire format.

The fake is a real subprocess writing real JSONL to a real pipe, so these
exercise the parts that actually break -- framing, frame ordering, which field
an outcome is read from -- without a model call, a bill, or a nondeterministic
answer.

Three of these guard mistakes the stream's shape invites: the `result` frame's
own `usage` covers only the last request to the main model, a settled run that
was denied a tool is still a settled run, and a run that ends without a
`result` frame did not finish no matter what it printed on the way.
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
from typing import Any

import pytest
from fine_agent import AGENT_QUESTION_OPT_IN_KEY
from fine_agent.history_types import (
    AgentBackend,
    HistoryDiscoveryStatus,
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
from finecode_extension_api.interfaces.iuserprompt import (
    ElicitationOutcome,
    ElicitationResult,
    IUserPrompt,
    PromptAvailability,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner import partial_result_sender
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session, run_handler
from finecode_extension_runner.testing._stubs import FakeUserPrompt

from fine_agent_claude_code import claude_code_agent_handler
from fine_agent_claude_code.claude_code_agent_handler import (
    ClaudeCodeAgentHandler,
    ClaudeCodeAgentHandlerConfig,
    ClaudeCodeAgentProfile,
)
from fine_agent_claude_code.run_claude_code_agent_task_action import (
    RunClaudeCodeAgentTaskAction,
)

_FAKE_CLAUDE = pathlib.Path(__file__).parent / "fake_claude.py"


_ACTIVE_SCENARIO: dict[str, Any] = {}


def _discard_partial(
    _token: int | str,
    _result: code_action.RunActionResult,
    _formats: list[str] | None,
) -> None:
    return None


class _FakeClaudeHandler(ClaudeCodeAgentHandler):
    """The real handler with the fake CLI swapped in for the `claude` binary.

    Module level because `run_handler` resolves a handler by its import path, so
    a class defined inside a test function cannot be loaded. The scenario
    therefore travels through `_ACTIVE_SCENARIO` rather than a closure -- adding
    an `executable` field to the production config would be the tidier-looking
    option and the wrong one, since pointing the handler at an arbitrary binary
    is not something configuration should be able to do.
    """

    def _executable(self) -> list[str]:
        return [sys.executable, str(_FAKE_CLAUDE), json.dumps(_ACTIVE_SCENARIO)]


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
    start_without_questions: bool = False,
    project_dir: pathlib.Path | None = None,
    action_cls: Any = RunAgentTaskAction,
) -> RunAgentTaskRunResult:
    global _ACTIVE_SCENARIO
    _ACTIVE_SCENARIO = scenario
    try:
        result = await run_handler(
            _FakeClaudeHandler,
            RunAgentTaskRunPayload(
                prompt=prompt,
                profile=profile,
                output_schema=output_schema,
                capture_history=capture_history,
                continue_session=continue_session,
                start_without_questions=start_without_questions,
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


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use(name: str) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "id": "toolu_1", "input": {}}


def _result(
    *,
    subtype: str = "success",
    is_error: bool = False,
    text: str = "",
    turns: int = 1,
    denials: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "num_turns": turns,
        "permission_denials": denials or [],
        **extra,
    }


async def test_snapshot_terminal_matches_final_result_and_preserves_rewritten_output(
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
    handler_source = (
        f"{_FakeClaudeHandler.__module__}.{_FakeClaudeHandler.__qualname__}"
    )
    actions = {
        RunAgentTaskAction.__name__: {
            "source": action_source,
            "handlers": [{"name": "claude", "source": handler_source}],
        }
    }
    scenario = {
        "steps": [
            {"do": "read_prompt"},
            {"do": "emit", "frame": _assistant(_text("partial answer"))},
            {
                "do": "emit",
                "frame": _result(text="canonical answer", turns=3),
            },
        ]
    }
    _ACTIVE_SCENARIO.clear()
    _ACTIVE_SCENARIO.update(scenario)
    try:
        async with handler_test_session(
            project_dir=tmp_path, actions=actions
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
    assert token_result.output == "canonical answer"
    assert token_events[-1] == token_result
    assert token_events[-1].output == "canonical answer"
    assert token_events[0].status is AgentRunStatus.RUNNING
    assert token_events[0].output == "partial answer"
    assert token_events[0].usage is None and token_events[0].session is None


def _model_usage(**models: dict[str, Any]) -> dict[str, Any]:
    return {"modelUsage": models}


async def test_reports_the_final_text_and_settles() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _assistant(_text("Hello"))},
                {"do": "emit", "frame": _result(text="Hello, world", turns=3)},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    # From the `result` frame, not the streamed assistant text: the CLI has
    # already assembled the answer there, and the frames it streamed on the way
    # are interleaved with tool calls.
    assert result.output == "Hello, world"
    assert result.turns == 3
    assert result.error is None
    # Measured by the handler, so it survives a backend that reports nothing.
    assert result.duration_sec is not None


async def test_sends_the_prompt_on_stdin(tmp_path: pathlib.Path) -> None:
    """Not in the command line: the prompt is arbitrary user text of arbitrary
    length, and stdin has neither a length limit nor a quoting problem."""
    record = tmp_path / "record.jsonl"
    await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "read_prompt"},
                {"do": "emit", "frame": _result(text="ok")},
            ],
        },
        prompt="refactor the parser",
    )

    assert _records(record) == [{"prompt": "refactor the parser"}]


async def test_a_stream_without_a_result_frame_is_not_a_finished_run() -> None:
    """The CLI died mid-run. The text it produced first is still the best
    account of what happened, but reporting SETTLED would tell the caller a
    truncated run finished its work."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _assistant(_text("halfway through"))},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "halfway through"
    assert result.error == "claude stopped without reporting a result"


async def test_an_error_subtype_fails_and_names_the_reason() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {
                    "do": "emit",
                    "frame": _result(
                        subtype="error_during_execution",
                        is_error=True,
                        text="the model call was rejected",
                    ),
                },
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error is not None
    assert "error_during_execution" in result.error
    assert "the model call was rejected" in result.error
    # The same field carries the answer on a settled run and the reason on a
    # failed one; reporting it as output would present an error as the agent's
    # work product.
    assert result.output == ""


async def test_a_denied_tool_on_a_failed_run_is_a_refusal_not_a_failure() -> None:
    """The distinction a non-interactive caller acts on: nothing went wrong,
    the run needed a decision this setup was configured not to make."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {
                    "do": "emit",
                    "frame": _result(
                        subtype="error_during_execution",
                        is_error=True,
                        denials=[{"tool_name": "Edit"}],
                    ),
                },
            ]
        }
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "Edit" in result.error


async def test_a_denied_tool_on_a_settled_run_is_still_a_settled_run() -> None:
    """The agent asked, was told no, and found another way. Reporting that as
    a refusal would fail a run that produced exactly what was asked for."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {
                    "do": "emit",
                    "frame": _result(
                        text="done without it", denials=[{"tool_name": "Bash"}]
                    ),
                },
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.error is None


async def test_usage_totals_every_model_the_run_used() -> None:
    """`modelUsage`, not the frame's own `usage`: the latter reports only the
    last request to the main model, so a run that delegated to a cheaper model
    for its own bookkeeping would under-report what it spent."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {
                    "do": "emit",
                    "frame": _result(
                        text="done",
                        total_cost_usd=0.0334,
                        usage={"input_tokens": 2, "output_tokens": 4},
                        **_model_usage(
                            **{
                                "claude-opus-5": {
                                    "inputTokens": 2,
                                    "outputTokens": 4,
                                    "cacheReadInputTokens": 0,
                                    "cacheCreationInputTokens": 3275,
                                    "canonicalModel": "claude-opus-5",
                                    "provider": "firstParty",
                                },
                                "claude-haiku-4-5": {
                                    "inputTokens": 521,
                                    "outputTokens": 13,
                                    "cacheReadInputTokens": 0,
                                    "cacheCreationInputTokens": 0,
                                    "canonicalModel": "claude-haiku-4-5",
                                    "provider": "firstParty",
                                },
                            }
                        ),
                    ),
                },
            ]
        }
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 523
    assert result.usage.output_tokens == 17
    assert result.usage.cache_write_tokens == 3275
    assert result.usage.approx_cost_usd == 0.0334
    assert result.usage.provider == "firstParty"
    # Two models ran, so there is no one model the cost can be attributed to.
    assert result.usage.model is None
    # Never derived: the CLI reports no total, and input plus output is not one.
    assert result.usage.total_tokens is None


async def test_usage_falls_back_to_the_frames_own_totals() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {
                    "do": "emit",
                    "frame": _result(
                        text="done",
                        total_cost_usd=0.5,
                        usage={
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 30,
                        },
                    ),
                },
            ]
        }
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 30
    assert result.usage.approx_cost_usd == 0.5
    # Named by the init frame, which is the only place a single-model run says
    # what produced the figures.
    assert result.usage.model == "claude-opus-5"


async def test_a_backend_reporting_no_usage_leaves_it_unset() -> None:
    """`None` rather than a zeroed `AgentRunUsage`: nothing was reported, which
    is not the same as a run that reported nothing was spent."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _result(text="done")},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.usage is None


async def test_a_non_json_line_does_not_end_the_run() -> None:
    """Strict JSONL is documented, but a stray warning on stdout must not fail
    a run that is otherwise fine."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit_raw", "text": "warning: something\n"},
                {"do": "emit", "frame": _result(text="fine")},
            ]
        }
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "fine"


async def test_tool_calls_do_not_leak_into_the_answer() -> None:
    """R-304 in the other direction: a tool call is narrative, and the answer
    is only the text the agent wrote."""
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _assistant(_tool_use("Bash"))},
                {"do": "emit", "frame": _assistant(_text("streamed"))},
                {"do": "emit", "frame": _result(text="final answer")},
            ]
        }
    )

    assert result.output == "final answer"


async def test_a_nonzero_exit_fails_the_run_with_stderr() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "emit", "frame": _result(text="looks fine")},
                {"do": "stderr", "text": "auth expired"},
                {"do": "exit", "code": 1},
            ]
        }
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error is not None
    assert "auth expired" in result.error
    # The text the run produced before dying is kept: it is still the best
    # account of what happened.
    assert result.output == "looks fine"


async def test_a_run_that_outlives_its_timeout_fails() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "sleep", "seconds": 30},
            ]
        },
        handler_config={"settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error is not None
    assert "did not finish within" in result.error


def _recorded_argv(record: pathlib.Path) -> list[str]:
    return next(entry["argv"] for entry in _records(record) if "argv" in entry)


def _flag_value(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _settled_scenario(record: pathlib.Path) -> dict[str, Any]:
    return {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "read_prompt"},
            {"do": "init"},
            {"do": "emit", "frame": _result(text="done")},
        ],
    }


@pytest.mark.parametrize(
    "action_cls", [RunAgentTaskAction, RunClaudeCodeAgentTaskAction]
)
async def test_profile_selects_its_model(
    tmp_path: pathlib.Path, action_cls: Any
) -> None:
    """A profile is the only way one task runs on a different model than
    another, so its model must reach the command."""
    record = tmp_path / "record.jsonl"
    result = await _run(
        _settled_scenario(record),
        handler_config={"model": "M1", "profiles": {"p": {"model": "M2"}}},
        profile="p",
        action_cls=action_cls,
    )

    assert isinstance(result, RunAgentTaskRunResult)
    assert result.status is AgentRunStatus.SETTLED

    assert _flag_value(_recorded_argv(record), "--model") == "M2"


def test_build_command_passes_thinking_as_effort() -> None:
    argv = _cmd(thinking="high")
    assert argv[argv.index("--effort") + 1] == "high"


def test_build_command_omits_effort_when_thinking_is_unset() -> None:
    assert "--effort" not in _cmd()


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
        assert _flag_value(_recorded_argv(record), "--effort") == expected


async def test_no_profile_uses_the_top_level_model(tmp_path: pathlib.Path) -> None:
    """An existing caller that sends no profile must get exactly today's command."""
    record = tmp_path / "record.jsonl"
    await _run(
        _settled_scenario(record),
        handler_config={"model": "M1", "profiles": {"p": {"model": "M2"}}},
    )

    assert _flag_value(_recorded_argv(record), "--model") == "M1"


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
            "claude", None, FileNotFoundError(2, "No such file or directory")
        ),
        icommandrunner.ProgramStartError(
            "claude", None, PermissionError(13, "Permission denied")
        ),
        icommandrunner.UnsafeBatchArgumentError(
            "argument 1 cannot be passed safely to batch file pi.cmd"
        ),
        icommandrunner.UnlaunchableProgramError(
            "pi resolves to pi.js, which CreateProcess cannot launch"
        ),
    ],
)
async def test_a_run_that_cannot_spawn_claude_is_a_structured_failure(
    error: Exception,
) -> None:
    """An unstartable claude -- missing from PATH, or a Windows shim refusing
    an argument -- is a structured `FAILED` result naming the program and the
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
            "claude", None, FileNotFoundError(2, "No such file or directory")
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
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "sleep", "seconds": 300},
            ]
        },
        handler_config={
            "settle_timeout_sec": 900,
            "profiles": {"p": {"settle_timeout_sec": 0.5}},
        },
        profile="p",
    )

    assert result.status is AgentRunStatus.FAILED
    assert "0.5s" in (result.error or "")


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
}


def _schema_scenario(
    record: pathlib.Path | None = None,
    **result_extra: Any,
) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "steps": [
            {"do": "read_prompt"},
            {"do": "init"},
            {
                "do": "emit",
                "frame": _result(text=result_extra.pop("text", "done"), **result_extra),
            },
        ]
    }
    if record is not None:
        scenario["record_path"] = str(record)
        scenario["record_driver"] = True
    return scenario


async def test_output_schema_is_passed_as_compact_json_on_the_argv(
    tmp_path: pathlib.Path,
) -> None:
    """The CLI takes the schema as a flag, and compact JSON is what it expects."""
    record = tmp_path / "record.jsonl"
    await _run(
        _schema_scenario(record, structured_output={"answer": "42"}),
        output_schema=_SCHEMA,
    )

    argv = _recorded_argv(record)
    assert argv[argv.index("--json-schema") + 1] == json.dumps(_SCHEMA)


@pytest.mark.parametrize(
    "action_cls", [RunAgentTaskAction, RunClaudeCodeAgentTaskAction]
)
async def test_structured_output_is_returned(action_cls: Any) -> None:
    result = await _run(
        _schema_scenario(structured_output={"answer": "42"}),
        output_schema=_SCHEMA,
        action_cls=action_cls,
    )

    assert isinstance(result, RunAgentTaskRunResult)

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"answer": "42"}


async def test_settling_without_structured_output_is_a_failure() -> None:
    """A settled run with no structured output must not hand the caller `None`
    where it asked for a report."""
    result = await _run(_schema_scenario(), output_schema=_SCHEMA)

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "claude settled without structured output"
    assert result.output == "done"
    assert result.session is None


async def test_structured_output_retry_exhaustion_is_a_failure() -> None:
    """The CLI has its own subtype for failing to satisfy the schema, and it is
    not a success just because the process exited cleanly."""
    result = await _run(
        _schema_scenario(
            subtype="error_max_structured_output_retries",
            is_error=True,
            text="could not satisfy the schema",
        ),
        output_schema=_SCHEMA,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "error_max_structured_output_retries" in (result.error or "")


def _handler_with(**config: Any) -> ClaudeCodeAgentHandler:
    handler = ClaudeCodeAgentHandler.__new__(ClaudeCodeAgentHandler)
    defaults: dict[str, Any] = {
        "model": None,
        "thinking": None,
        "permission_mode": None,
        "allowed_tools": [],
        "disallowed_tools": [],
        "append_system_prompt": None,
        "max_budget_usd": None,
    }
    handler.config = type("_Config", (), defaults | config)()
    return handler


def _settings(**values: Any) -> claude_code_agent_handler._ClaudeRunSettings:
    return claude_code_agent_handler._ClaudeRunSettings(
        **{"model": None, "thinking": None, "settle_timeout_sec": 900.0} | values
    )


def _cmd(**config: Any) -> list[str]:
    """The real builder's argv for a config plus a resolved settings object.

    A profile moves `model` out of the config, so it travels through settings
    the way a run does.
    """
    config = dict(config)
    settings = _settings(
        model=config.pop("model", None), thinking=config.pop("thinking", None)
    )
    return ClaudeCodeAgentHandler._build_command(_handler_with(**config), settings)


def test_build_command_always_asks_for_the_machine_readable_stream() -> None:
    """The real command builder, which the fake-CLI tests deliberately replace.

    `--verbose` is load-bearing rather than decorative: the CLI rejects
    `stream-json` output in print mode without it.
    """
    assert _cmd() == [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def test_build_command_carries_configured_options() -> None:
    assert _cmd(
        model="opus",
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Bash(git *)"],
        disallowed_tools=["WebFetch"],
        max_budget_usd=1.5,
    )[5:] == [
        "--model",
        "opus",
        "--permission-mode",
        "acceptEdits",
        "--allowed-tools",
        "Read",
        "Bash(git *)",
        "--disallowed-tools",
        "WebFetch",
        "--max-budget-usd",
        "1.5",
    ]


@pytest.mark.parametrize(
    ("field", "value", "flag"),
    [
        ("model", "opus", "--model"),
        ("permission_mode", "acceptEdits", "--permission-mode"),
        ("allowed_tools", ["Read"], "--allowed-tools"),
        ("disallowed_tools", ["Read"], "--disallowed-tools"),
        ("append_system_prompt", "be terse", "--append-system-prompt"),
        ("max_budget_usd", 1.0, "--max-budget-usd"),
    ],
)
def test_build_command_omits_unset_options(field: str, value: Any, flag: str) -> None:
    assert flag in _cmd(**{field: value})
    assert flag not in _cmd()


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
    """The timeout has to stop the agent, not just stop waiting for it.

    A claude that outlives its own settle timeout keeps write access to files a
    caller may already be restoring, keeps spending budget, and keeps holding
    the subprocess slot the timeout exists to release. This one refuses the
    polite exit (SIGTERM ignored) and has a child of its own, so the whole
    ladder and the process group both have to work for it to end.
    """
    monkeypatch.setattr(claude_code_agent_handler, "_EXIT_GRACE_SEC", 0.3)
    monkeypatch.setattr(claude_code_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)

    record = tmp_path / "record.jsonl"
    result = await _run(
        {
            "record_path": str(record),
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "ignore_sigterm"},
                {"do": "spawn_child"},
                {"do": "sleep", "seconds": 300},
            ],
        },
        handler_config={"settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not finish within" in (result.error or "")

    pids = next(entry["pids"] for entry in _records(record) if "pids" in entry)
    assert await _wait_gone(pids["agent"]), "the agent outlived its own timeout"
    assert await _wait_gone(pids["child"]), "the agent's child was left running"


CAPTURED_SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _session_ref(
    project_dir: pathlib.Path,
    session_id: str = CAPTURED_SESSION_ID,
    backend: AgentBackend = AgentBackend.CLAUDE_CODE,
) -> AgentSessionRef:
    return AgentSessionRef(
        backend=backend,
        session_id=session_id,
        project_dir=path_to_resource_uri(project_dir),
    )


def _project_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)
    return project_dir


def _captured_scenario(
    config_dir: str, session_id: str = CAPTURED_SESSION_ID
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "config_dir": config_dir,
        "write_native": True,
        "native_proj": "testproj",
        "native_related": [
            {
                "path": "subagents/agent-x.jsonl",
                "records": [{"type": "user", "agentId": "x"}],
            }
        ],
        "steps": [
            {"do": "read_prompt"},
            {"do": "init", "session_id": session_id},
            {"do": "emit", "frame": _assistant(_text("hello"))},
            {
                "do": "emit",
                "frame": _result(
                    text="hello",
                    turns=1,
                    session_id=session_id,
                    modelUsage={"m": {"inputTokens": 10, "outputTokens": 5}},
                    total_cost_usd=0.001,
                ),
            },
        ],
    }


async def test_uncaptured_has_no_observation() -> None:
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _result(text="hi")},
            ]
        }
    )

    assert result.observation is None


async def test_captured_run_reports_available_history(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = await _run(_captured_scenario(str(tmp_path)), capture_history=True)

    assert result.status is AgentRunStatus.SETTLED
    observation = result.observation
    assert observation is not None
    assert observation.backend is AgentBackend.CLAUDE_CODE
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert observation.session_id == CAPTURED_SESSION_ID
    assert len(observation.files) == 2
    assert observation.related_status is RelatedHistoryStatus.SCANNED
    assert observation.usage_provenance.token_source is UsageSource.CLAUDE_MODELS
    assert observation.usage_provenance.token_scope is UsageScope.SESSION
    assert observation.backend_version == "2.1.306"


async def test_captured_result_fallback_uses_last_request(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    scenario = _captured_scenario(str(tmp_path))
    scenario["steps"] = [
        {"do": "read_prompt"},
        {"do": "init", "session_id": "sess-captured-1"},
        {
            "do": "emit",
            "frame": _result(
                text="hi",
                session_id="sess-captured-1",
                usage={"input_tokens": 7, "output_tokens": 3},
                total_cost_usd=0.002,
            ),
        },
    ]
    result = await _run(scenario, capture_history=True)

    assert result.observation is not None
    assert result.observation.usage_provenance.token_source is UsageSource.CLAUDE_RESULT
    assert result.observation.usage_provenance.token_scope is UsageScope.LAST_REQUEST
    assert result.observation.usage_provenance.cost_source is UsageSource.CLAUDE_COST


async def test_captured_session_disagreement_produces_gap(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disagreeing init/result ids leave no usable identity to report.

    The text the run produced is kept, but an otherwise-settled run whose
    frames name two different sessions cannot hand out either one.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    scenario = _captured_scenario(str(tmp_path))
    scenario["steps"] = [
        {"do": "read_prompt"},
        {"do": "init", "session_id": "sess-a"},
        {"do": "emit", "frame": _result(text="hi", session_id="sess-b")},
    ]
    result = await _run(scenario, capture_history=True)

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "hi"
    assert result.session is None
    assert result.observation is not None
    assert result.observation.session_id is None
    assert any("disagreement" in gap for gap in result.observation.gaps)


def _resume_scenario(
    record: pathlib.Path | None,
    session_id: str,
    text: str = "continued",
    **result_extra: Any,
) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "steps": [
            {"do": "read_prompt"},
            {"do": "init", "session_id": session_id},
            {
                "do": "emit",
                "frame": _result(text=text, session_id=session_id, **result_extra),
            },
        ]
    }
    if record is not None:
        scenario["record_path"] = str(record)
        scenario["record_driver"] = True
    return scenario


def _native_main(config_dir: pathlib.Path, session_id: str) -> pathlib.Path:
    matches = [
        path
        for path in (config_dir / "projects").rglob(f"{session_id}.jsonl")
        if path.is_file()
    ]
    assert len(matches) == 1
    return matches[0]


def _native_session_files(config_dir: pathlib.Path) -> set[str]:
    base = config_dir / "projects"
    if not base.is_dir():
        return set()
    return {str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()}


_CONTINUE_CONFIG = {"model": "opus", "permission_mode": "acceptEdits"}


async def test_settled_run_reports_session_and_continuations_reuse_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new settled run hands out its native session; later prompts resume it.

    A second prompt must reopen the same native session (not start one), and
    a third can reuse the reference the second run returned. Decoys and the
    session-file set are untouched by the resumes.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        prompt="first",
        project_dir=project_dir,
    )

    assert first.status is AgentRunStatus.SETTLED
    assert first.session == _session_ref(project_dir)
    assert (
        first.session is not None and first.session.backend is AgentBackend.CLAUDE_CODE
    )
    assert first.observation is None
    main_file = _native_main(tmp_path, CAPTURED_SESSION_ID)
    assert "PROMPT-1" in main_file.read_text(encoding="utf-8")
    files_before = _native_session_files(tmp_path)
    assert files_before
    decoy = (
        tmp_path
        / "projects"
        / "testproj"
        / CAPTURED_SESSION_ID
        / "subagents"
        / "agent-x.jsonl"
    )
    decoy_before = decoy.read_bytes()

    second = await _run(
        _resume_scenario(None, CAPTURED_SESSION_ID, text="second"),
        prompt="second",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    assert "second" in main_file.read_text(encoding="utf-8")
    assert _native_session_files(tmp_path) == files_before
    assert decoy.read_bytes() == decoy_before

    assert second.session is not None
    third = await _run(
        _resume_scenario(None, CAPTURED_SESSION_ID, text="third"),
        prompt="third",
        project_dir=project_dir,
        continue_session=second.session,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert third.status is AgentRunStatus.SETTLED
    assert third.session == first.session
    assert "third" in main_file.read_text(encoding="utf-8")
    assert _native_session_files(tmp_path) == files_before


async def test_continuation_argv_resumes_with_current_settings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume argv carries the current model, mode and lists, not saved ones."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config={"model": "old", "permission_mode": "acceptEdits"},
    )
    assert first.session is not None
    record = tmp_path / "resume-record.jsonl"
    second = await _run(
        _resume_scenario(record, CAPTURED_SESSION_ID, text="again"),
        prompt="again",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={
            "model": "new",
            "permission_mode": "bypassPermissions",
            "allowed_tools": ["Read"],
            "disallowed_tools": [],
            "append_system_prompt": "v2",
            "max_budget_usd": 2.5,
        },
    )

    assert second.status is AgentRunStatus.SETTLED
    driver = _recorded_argv(record)
    assert driver[:5] == [
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--resume",
    ]
    assert _flag_value(driver, "--resume") == CAPTURED_SESSION_ID
    assert _flag_value(driver, "--system-prompt-snapshot") == "off"
    assert _flag_value(driver, "--model") == "new"
    assert _flag_value(driver, "--permission-mode") == "bypassPermissions"
    assert _flag_value(driver, "--append-system-prompt") == "v2"
    assert _flag_value(driver, "--max-budget-usd") == "2.5"
    allowed = driver[driver.index("--allowed-tools") + 1]
    assert allowed == "Read"
    disallowed = driver[driver.index("--disallowed-tools") + 1]
    assert disallowed == ""
    assert "--fork-session" not in driver


async def test_continuation_removed_append_prompt_keeps_snapshot_off(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the append text still rebuilds instructions instead of restoring."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG, append_system_prompt="v1"),
    )
    assert first.session is not None
    record = tmp_path / "resume-no-append.jsonl"
    result = await _run(
        _resume_scenario(record, CAPTURED_SESSION_ID),
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.SETTLED
    driver = _recorded_argv(record)
    assert "--append-system-prompt" not in driver
    assert _flag_value(driver, "--system-prompt-snapshot") == "off"


def test_continuation_build_command_form() -> None:
    """Resume flags belong to the run; empty lists stay explicitly empty."""
    ref = _session_ref(pathlib.Path("/ws/proj"), session_id=CAPTURED_SESSION_ID)
    argv = _handler_with(
        permission_mode="acceptEdits", allowed_tools=[], disallowed_tools=[]
    )._build_command(_settings(model="opus"), None, continue_session=ref)
    assert argv[:8] == [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--resume",
        CAPTURED_SESSION_ID,
        "--system-prompt-snapshot",
    ]
    assert argv[8] == "off"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowed-tools") + 1] == ""
    assert argv[argv.index("--disallowed-tools") + 1] == ""
    assert "--fork-session" not in argv


def test_continuation_build_command_carries_non_empty_lists() -> None:
    ref = _session_ref(pathlib.Path("/ws/proj"), session_id=CAPTURED_SESSION_ID)
    argv = _handler_with(
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Bash(git *)"],
        disallowed_tools=["WebFetch"],
        append_system_prompt="be terse",
        max_budget_usd=1.5,
    )._build_command(_settings(model="opus"), None, continue_session=ref)
    assert argv[
        argv.index("--allowed-tools") + 1 : argv.index("--allowed-tools") + 3
    ] == [
        "Read",
        "Bash(git *)",
    ]
    assert argv[argv.index("--disallowed-tools") + 1] == "WebFetch"
    assert argv[argv.index("--append-system-prompt") + 1] == "be terse"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"


async def test_continuation_rejects_non_uuid_ids_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    for bad_id in ("", "sess-1", CAPTURED_SESSION_ID[:8]):
        runner = _RecordingCommandRunner()
        result = await _run(
            {"steps": []},
            project_dir=project_dir,
            continue_session=_session_ref(project_dir, session_id=bad_id),
            service_overrides={icommandrunner.ICommandRunner: runner},
        )
        assert result.status is AgentRunStatus.FAILED
        assert "session ID" in (result.error or "")
        assert runner.start_long_running_calls == 0
        assert runner.run_calls == 0


async def test_continuation_rejects_backend_mismatch_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=_session_ref(project_dir, backend=AgentBackend.CODEX),
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "codex" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_rejects_foreign_project_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    ref = AgentSessionRef(
        backend=AgentBackend.CLAUDE_CODE,
        session_id=CAPTURED_SESSION_ID,
        project_dir=ResourceUri("file:///elsewhere"),
    )
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=ref,
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "elsewhere" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_unknown_profile_fails_without_lookup_or_spawn(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        profile="nope",
        continue_session=_session_ref(project_dir),
        handler_config={"profiles": {"b": {}, "a": {}}},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "unknown agent profile 'nope'; configured profiles: a, b"
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_missing_session_fails_without_probe_or_spawn(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted session is reported, and no version subprocess is launched."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=_session_ref(project_dir),
        capture_history=True,
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "not found" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0
    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE


async def test_continuation_without_model_fails_before_spawn(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved session model is never silently reused on continuation."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config={"model": "opus", "permission_mode": "acceptEdits"},
    )
    assert first.session is not None
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"permission_mode": "acceptEdits"},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "model" in (result.error or "")
    assert "new-run default" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_without_permission_mode_fails_before_spawn(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native resume must not restore saved permissions the handler cannot name."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config={"model": "opus", "permission_mode": "acceptEdits"},
    )
    assert first.session is not None
    runner = _RecordingCommandRunner()
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "opus"},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "permission_mode" in (result.error or "")
    assert "new-run default" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_missing_session_fails_and_creates_no_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver re-reads the native store; a miss creates nothing."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    ref = _session_ref(project_dir, session_id="33333333-3333-4333-8333-333333333333")
    files_before = _native_session_files(tmp_path)
    record = tmp_path / "miss-record.jsonl"
    result = await _run(
        _resume_scenario(record, ref.session_id, text="hello?"),
        prompt="hello?",
        project_dir=project_dir,
        continue_session=ref,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.FAILED
    assert "not found" in (result.error or "")
    assert result.session is None
    assert _native_session_files(tmp_path) == files_before


async def test_continuation_deleted_session_fails_without_creating_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session removed after the handler lookup still fails at resume."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    files_before = _native_session_files(tmp_path)

    real_locate = claude_code_agent_handler.claude_code_history.locate_claude_history

    def _locate_then_delete(state: Any, root: pathlib.Path) -> Any:
        observation = real_locate(state, root)
        for path in (root / "projects").rglob("*.jsonl"):
            if path.is_file():
                path.unlink()
        return observation

    monkeypatch.setattr(
        claude_code_agent_handler.claude_code_history,
        "locate_claude_history",
        _locate_then_delete,
    )
    result = await _run(
        _resume_scenario(None, CAPTURED_SESSION_ID, text="hello?"),
        prompt="hello?",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.FAILED
    assert "stopped without reporting" in (result.error or "")
    assert result.session is None
    assert _native_session_files(tmp_path) <= files_before


async def test_continuation_settled_without_reported_id_fails_and_keeps_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed turn that names no session fails without losing its text."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "emit", "frame": _result(text="hi")},
            ]
        },
        capture_history=True,
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "hi"
    assert result.session is None
    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE
    assert result.observation.session_id is None


async def test_continuation_refusal_without_reported_id_stays_refusal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal that names no session is still a refusal, not a new failure."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {
                    "do": "emit",
                    "frame": _result(
                        subtype="error_during_execution",
                        is_error=True,
                        denials=[{"tool_name": "Edit"}],
                    ),
                },
            ]
        }
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None and "Edit" in result.error
    assert result.session is None


async def test_continuation_structured_output_round_trip(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed turn extracts JSON against the schema like a new turn does."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    record = tmp_path / "structured-resume-record.jsonl"
    result = await _run(
        _resume_scenario(
            record,
            CAPTURED_SESSION_ID,
            structured_output={"answer": "42"},
        ),
        project_dir=project_dir,
        continue_session=first.session,
        output_schema=_SCHEMA,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"answer": "42"}
    assert result.session == first.session


async def test_continuation_malformed_structured_output_fails_and_keeps_text(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    result = await _run(
        _resume_scenario(record=None, session_id=CAPTURED_SESSION_ID, text="done"),
        project_dir=project_dir,
        continue_session=first.session,
        output_schema=_SCHEMA,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "claude settled without structured output"
    assert result.output == "done"
    assert result.session is None


async def test_continuation_backend_failure_keeps_its_reason(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init", "session_id": CAPTURED_SESSION_ID},
                {
                    "do": "emit",
                    "frame": _result(
                        subtype="error_during_execution",
                        is_error=True,
                        text="the model call was rejected",
                        session_id=CAPTURED_SESSION_ID,
                    ),
                },
            ]
        },
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.FAILED
    assert "error_during_execution" in (result.error or "")
    assert result.session is None


async def test_continuation_timeout_fails_without_a_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run that hangs is bounded by the current timeout setting."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    result = await _run(
        {
            "steps": [
                {"do": "read_prompt"},
                {"do": "init", "session_id": CAPTURED_SESSION_ID},
                {"do": "sleep", "seconds": 300},
            ]
        },
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG, settle_timeout_sec=0.5),
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not finish within" in (result.error or "")
    assert result.session is None


class _PipeBrokenProcess:
    """A native process that exits before reading the prompt."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def stdout_lines(self) -> Any:
        if False:
            yield ""
        return

    def write_to_stdin(self, value: str) -> None:
        _ = value
        raise self._error

    def close_stdin(self) -> None:
        return None

    async def wait_for_end(self, timeout: float | None = None) -> None:
        _ = timeout

    def get_exit_code(self) -> int | None:
        return 0

    def get_output(self) -> str:
        raise AssertionError("streaming forbids get_output")

    def get_error_output(self) -> str:
        return ""

    def is_alive(self) -> bool:
        return False


class _PipeBrokenRunner:
    """Serves one broken process for the spawn a continuation performs."""

    def __init__(self, process: _PipeBrokenProcess) -> None:
        self._process = process
        self.start_calls = 0

    async def start_long_running(
        self,
        cmd: Any,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        _ = (cmd, cwd, env, new_process_group)
        self.start_calls += 1
        return self._process

    async def run(
        self,
        cmd: Any,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        _ = (cmd, cwd, env, new_process_group)
        raise AssertionError("no version probe runs on this path")

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

    def run_sync(
        self,
        cmd: Any,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        _ = (cmd, cwd, env)
        raise NotImplementedError


@pytest.mark.parametrize(
    "error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")]
)
async def test_continuation_prompt_pipe_failure_is_an_explicit_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A native process that exits before reading the prompt fails the turn."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    result = await _run(
        {"steps": []},
        project_dir=project_dir,
        continue_session=first.session,
        handler_config=dict(_CONTINUE_CONFIG),
        service_overrides={
            icommandrunner.ICommandRunner: _PipeBrokenRunner(_PipeBrokenProcess(error))
        },
    )

    assert result.status is AgentRunStatus.FAILED
    assert "pipe" in (result.error or "")
    assert result.session is None


async def test_continuation_capture_locates_the_same_main_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture on a continuation observes the resumed native session."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    result = await _run(
        _resume_scenario(None, CAPTURED_SESSION_ID, text="more"),
        prompt="more",
        project_dir=project_dir,
        continue_session=first.session,
        capture_history=True,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.session == first.session
    observation = result.observation
    assert observation is not None
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert observation.session_id == CAPTURED_SESSION_ID
    main = next(f for f in observation.files if f.role is NativeHistoryRole.MAIN)
    assert resource_uri_to_path(main.source_uri) == _native_main(
        tmp_path, CAPTURED_SESSION_ID
    )


async def test_continuation_observation_failure_keeps_reference_and_status(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken history read degrades the observation, not the settled turn."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    first = await _run(
        _captured_scenario(str(tmp_path)),
        project_dir=project_dir,
        handler_config=dict(_CONTINUE_CONFIG),
    )
    assert first.session is not None
    calls: list[int] = []
    real_locate = claude_code_agent_handler.claude_code_history.locate_claude_history

    def _flaky_locate(state: Any, root: pathlib.Path) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return real_locate(state, root)
        raise RuntimeError("disk is gone")

    monkeypatch.setattr(
        claude_code_agent_handler.claude_code_history,
        "locate_claude_history",
        _flaky_locate,
    )
    result = await _run(
        _resume_scenario(None, CAPTURED_SESSION_ID, text="more"),
        prompt="more",
        project_dir=project_dir,
        continue_session=first.session,
        capture_history=True,
        handler_config=dict(_CONTINUE_CONFIG),
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "more"
    assert result.session == first.session
    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.FAILED


def _capable_prompt() -> Any:

    return FakeUserPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )


def test_opt_in_key_matches_shared_key() -> None:
    """The handler config field is the shared opt-in key (D6.declare)."""
    assert AGENT_QUESTION_OPT_IN_KEY == "may_ask_questions"
    assert "may_ask_questions" in ClaudeCodeAgentHandlerConfig.__dataclass_fields__


def test_profile_inherits_and_overrides_opt_in() -> None:
    """A profile inherits the top-level opt-in when unset, like the timeout."""
    handler = _handler_with(
        settle_timeout_sec=900.0,
        may_ask_questions=False,
        profiles={"q": ClaudeCodeAgentProfile()},
    )
    top = handler._resolve(None)
    assert isinstance(top, claude_code_agent_handler._ClaudeRunSettings)
    assert top.may_ask_questions is False
    handler2 = _handler_with(
        settle_timeout_sec=900.0,
        may_ask_questions=True,
        profiles={"q": ClaudeCodeAgentProfile()},
    )
    inherited = handler2._resolve("q")
    assert isinstance(inherited, claude_code_agent_handler._ClaudeRunSettings)
    assert inherited.may_ask_questions is True
    handler3 = _handler_with(
        settle_timeout_sec=900.0,
        may_ask_questions=True,
        profiles={"q": ClaudeCodeAgentProfile(may_ask_questions=False)},
    )
    overridden = handler3._resolve("q")
    assert isinstance(overridden, claude_code_agent_handler._ClaudeRunSettings)
    assert overridden.may_ask_questions is False


async def test_not_opted_in_runs_todays_argv_with_no_checks(
    tmp_path: pathlib.Path,
) -> None:
    """A run that did not opt in never probes and asks nothing."""

    record = tmp_path / "record.jsonl"
    prompt = _capable_prompt()
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _result(text="done")},
            ],
        },
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.availability_checks == 0
    assert prompt.asks == []
    records = _records(record)
    argv = next(entry["argv"] for entry in records if "argv" in entry)
    assert "--permission-prompt-tool" not in argv
    assert "--input-format" not in argv
    assert (
        next(entry["prompt"] for entry in records if "prompt" in entry)
        == "do the thing"
    )


async def test_opted_in_with_override_runs_todays_path_with_one_check(
    tmp_path: pathlib.Path,
) -> None:
    """An overridden run probes exactly once and then runs without questions."""

    record = tmp_path / "record.jsonl"
    prompt = FakeUserPrompt(
        availability=PromptAvailability(can_ask=False),
    )
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_prompt"},
                {"do": "init"},
                {"do": "emit", "frame": _result(text="done")},
            ],
        },
        handler_config={"may_ask_questions": True},
        start_without_questions=True,
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.availability_checks == 1
    assert prompt.asks == []
    records = _records(record)
    argv = next(entry["argv"] for entry in records if "argv" in entry)
    assert "--permission-prompt-tool" not in argv


async def test_opted_in_refused_when_nobody_can_answer(tmp_path: pathlib.Path) -> None:
    """A refused run never spawns claude and names the override."""

    record = tmp_path / "record.jsonl"
    prompt = FakeUserPrompt(availability=PromptAvailability(can_ask=False))
    result = await _run(
        {"record_path": str(record), "steps": [{"do": "read_prompt"}]},
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "start_without_questions" in result.error
    assert "CI" not in result.error
    assert not record.exists()
    assert prompt.availability_checks == 1


async def test_opted_in_refused_when_shapes_missing() -> None:
    """A refusal names the missing shapes, not the CI fallback."""

    prompt = FakeUserPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=False, can_ask_multi_choice=True
        )
    )
    result = await _run(
        {"steps": [{"do": "read_prompt"}]},
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "text" in result.error
    assert "start_without_questions" in result.error
    assert "did not declare" not in result.error


async def test_opted_in_refused_with_older_runner_stub() -> None:
    """A minimal prompt that only answers choices still refuses cleanly."""

    class _OldPrompt:
        async def check_availability(self) -> Any:
            return {"not": "a PromptAvailability"}

        async def ask_choice(self, *args: Any, **kwargs: Any) -> Any:
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

    result = await _run(
        {"steps": [{"do": "read_prompt"}]},
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: _OldPrompt()},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION


async def test_refusal_with_capture_carries_observation_and_no_probe() -> None:
    """A refused capture names the start check and never probes a version."""

    prompt = FakeUserPrompt(availability=PromptAvailability(can_ask=False))
    result = await _run(
        {"steps": [{"do": "read_prompt"}]},
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: prompt},
        capture_history=True,
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.observation is not None
    assert result.observation.gaps == [
        "refused at the question start check; claude was not started"
    ]
    assert result.observation.backend_version is None


def _wire_steps(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"steps": list(steps)}


def _wire_ask_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Which codename should I use?",
                "header": "Codename",
                "multiSelect": False,
                "options": [
                    {"label": "Quokka-7", "description": "Use the codename Quokka-7."},
                    {
                        "label": "Narwhal-3",
                        "description": "Use the codename Narwhal-3.",
                    },
                ],
            }
        ]
    }


def _wire_scenario(
    record: pathlib.Path,
    request_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    result_text: str = "Quokka-7",
    *,
    tool_use_id: str | None = "toolu_1",
    extra_steps: list[dict[str, Any]] | None = None,
    denials: list[dict[str, Any]] | None = None,
    result_subtype: str = "success",
    result_is_error: bool = False,
) -> dict[str, Any]:
    return {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "read_initialize"},
            {"do": "init"},
            {"do": "read_user"},
            {
                "do": "request",
                "request_id": request_id,
                "tool_name": tool_name,
                "input": tool_input,
                **({"tool_use_id": tool_use_id} if tool_use_id is not None else {}),
            },
            *(extra_steps or []),
            {"do": "await_response", "request_id": request_id, "within": 20},
            {
                "do": "emit",
                "frame": _result(
                    text=result_text,
                    denials=denials or [],
                    subtype=result_subtype,
                    is_error=result_is_error,
                ),
            },
            {"do": "await_stdin_eof"},
        ],
    }


async def _wire_run(
    scenario: dict[str, Any],
    answers: list[Any],
    handler_config: dict[str, Any] | None = None,
    start_without_questions: bool = False,
) -> tuple[RunAgentTaskRunResult, Any]:

    prompt = FakeUserPrompt(
        answers=answers,
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        ),
    )
    config = {"may_ask_questions": True}
    config.update(handler_config or {})
    result = await _run(
        scenario,
        handler_config=config,
        start_without_questions=start_without_questions,
        service_overrides={IUserPrompt: prompt},
    )
    return result, prompt


async def test_question_mode_handshake_order_and_argv(tmp_path: pathlib.Path) -> None:
    """The wire starts with initialize, then the user frame; stdin closes after result."""

    record = tmp_path / "record.jsonl"
    result, _ = await _wire_run(
        _wire_scenario(record, "r1", "AskUserQuestion", _wire_ask_input()),
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Quokka-7")],
    )

    assert result.status is AgentRunStatus.SETTLED
    records = _records(record)
    kinds = [next(iter(entry)) for entry in records]
    assert kinds[0] == "argv"
    argv = records[0]["argv"]
    verbose_at = argv.index("--verbose")
    assert argv[verbose_at + 1 : verbose_at + 5] == [
        "--input-format",
        "stream-json",
        "--permission-prompt-tool",
        "stdio",
    ]
    assert kinds[1] == "initialize"
    assert records[1]["initialize"]["request"] == {
        "subtype": "initialize",
        "hooks": None,
    }
    assert kinds[2] == "user"
    assert records[2]["user"]["message"] == {
        "role": "user",
        "content": "do the thing",
    }
    assert records[3] == {
        "request": {
            "type": "control_request",
            "request_id": "r1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": _wire_ask_input(),
                "tool_use_id": "toolu_1",
            },
        }
    }
    assert kinds[-1] == "stdin_eof"
    response = next(entry["response"] for entry in records if "response" in entry)
    assert response["response"]["request_id"] == "r1"


async def test_single_question_round_trip(tmp_path: pathlib.Path) -> None:
    """AC-1: one ask, labels plus Other, recorded allow with the answers."""

    record = tmp_path / "record.jsonl"
    result, prompt = await _wire_run(
        _wire_scenario(record, "r1", "AskUserQuestion", _wire_ask_input()),
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Quokka-7")],
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "Quokka-7"
    assert len(prompt.asks) == 1
    ask = prompt.asks[0]
    assert ask.message == "Codename: Which codename should I use?"
    assert list(ask.options) == ["Quokka-7", "Narwhal-3", "Other (type an answer)"]
    assert ask.descriptions is not None
    assert ask.descriptions.get("Quokka-7") == "Use the codename Quokka-7."
    assert ask.timeout_sec == 900.0
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    reply = response["response"]["response"]
    assert reply["behavior"] == "allow"
    assert reply["updatedInput"]["questions"] == _wire_ask_input()["questions"]
    assert reply["updatedInput"]["answers"] == {
        "Which codename should I use?": "Quokka-7"
    }


async def test_group_multi_and_other_end_to_end(tmp_path: pathlib.Path) -> None:
    """AC-2/3 shape: group of entries with multiSelect and Other reaches the fake."""

    record = tmp_path / "record.jsonl"
    tool_input: dict[str, Any] = {
        "questions": [
            {
                "question": "Pick one?",
                "options": [{"label": "A"}, {"label": "B"}],
                "multiSelect": False,
            },
            {
                "question": "Pick any?",
                "options": [{"label": "X"}, {"label": "Y"}],
                "multiSelect": True,
            },
        ]
    }
    result, prompt = await _wire_run(
        _wire_scenario(record, "r1", "AskUserQuestion", tool_input),
        [
            ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="B"),
            ElicitationResult(
                outcome=ElicitationOutcome.ANSWERED,
                values=("X", "Other (type an answer)"),
            ),
            ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="mine, yours"),
        ],
    )

    assert result.status is AgentRunStatus.SETTLED
    assert len(prompt.asks) == 3
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    reply = response["response"]["response"]
    assert reply["behavior"] == "allow"
    assert reply["updatedInput"]["answers"] == {
        "Pick one?": "B",
        "Pick any?": "X, mine, yours",
    }


async def test_not_supported_question_is_denied_and_excluded(
    tmp_path: pathlib.Path,
) -> None:
    """A repeated label is not asked; the run settles, and a later failure is just FAILED."""

    record = tmp_path / "record.jsonl"
    bad: dict[str, Any] = {
        "questions": [{"question": "Q?", "options": [{"label": "A"}, {"label": "A"}]}]
    }
    result, prompt = await _wire_run(
        _wire_scenario(
            record,
            "r1",
            "AskUserQuestion",
            bad,
            result_text="done anyway",
        ),
        [],
    )

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.asks == []
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    reply = response["response"]["response"]
    assert reply["behavior"] == "deny"
    assert "not an answer" in reply["message"]

    record2 = tmp_path / "record2.jsonl"
    result2, _ = await _wire_run(
        _wire_scenario(
            record2,
            "r1",
            "AskUserQuestion",
            bad,
            denials=[{"tool_name": "AskUserQuestion", "tool_use_id": "toolu_1"}],
            result_subtype="error_during_execution",
            result_is_error=True,
        ),
        [],
    )
    assert result2.status is AgentRunStatus.FAILED
    assert result2.error is not None
    assert "error_during_execution" in result2.error


async def test_permission_allow_passes_original_input(tmp_path: pathlib.Path) -> None:
    """An allowed call carries the original input and no permission widening."""

    record = tmp_path / "record.jsonl"
    tool_input: dict[str, Any] = {"file_path": "probe.txt", "content": "hello"}
    result, prompt = await _wire_run(
        _wire_scenario(record, "r1", "Write", tool_input),
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Allow once")],
    )

    assert result.status is AgentRunStatus.SETTLED
    assert len(prompt.asks) == 1
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    reply = response["response"]["response"]
    assert reply == {"behavior": "allow", "updatedInput": tool_input}
    assert "updatedPermissions" not in json.dumps(response)


async def test_permission_deny_then_crash_is_failed(tmp_path: pathlib.Path) -> None:
    """A person-denied call that later fails is FAILED, not a refusal."""

    record = tmp_path / "record.jsonl"
    result, _ = await _wire_run(
        _wire_scenario(
            record,
            "r1",
            "Write",
            {"file_path": "x"},
            denials=[{"tool_name": "Write", "tool_use_id": "toolu_1"}],
            result_subtype="error_during_execution",
            result_is_error=True,
        ),
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Deny")],
    )

    assert result.status is AgentRunStatus.FAILED


async def test_permission_decline_then_settle_is_settled(
    tmp_path: pathlib.Path,
) -> None:
    """A declined ask on a run that settles anyway stays settled (D1.G2)."""

    record = tmp_path / "record.jsonl"
    result, _ = await _wire_run(
        _wire_scenario(record, "r1", "Write", {"file_path": "x"}),
        [ElicitationResult(outcome=ElicitationOutcome.DECLINED)],
    )

    assert result.status is AgentRunStatus.SETTLED


async def test_unrouted_denial_still_refuses(tmp_path: pathlib.Path) -> None:
    """A decided denial plus an undecided one is still a refusal."""

    record = tmp_path / "record.jsonl"
    scenario = _wire_scenario(
        record,
        "r1",
        "Write",
        {"file_path": "x"},
        denials=[
            {"tool_name": "Write", "tool_use_id": "toolu_1"},
            {"tool_name": "Bash", "tool_use_id": "toolu_9"},
        ],
        result_subtype="error_during_execution",
        result_is_error=True,
    )
    result, _ = await _wire_run(
        scenario,
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Deny")],
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION


async def test_disallowed_tools_denied_without_asks(tmp_path: pathlib.Path) -> None:
    """Disallowed patterns never reach the person, parenthesised or not."""
    record = tmp_path / "record.jsonl"
    result, prompt = await _wire_run(
        _wire_scenario(record, "r1", "Bash", {"command": "rm *"}),
        [],
        handler_config={"disallowed_tools": ["Write", "Bash(rm *)"]},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.asks == []
    assert prompt.availability_checks == 1
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    assert response["response"]["response"]["behavior"] == "deny"


async def test_unknown_control_subtype_gets_error_reply(tmp_path: pathlib.Path) -> None:
    """An unknown subtype is answered with an error and the run continues."""
    record = tmp_path / "record.jsonl"
    scenario = {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "read_initialize"},
            {"do": "init"},
            {"do": "read_user"},
            {
                "do": "emit",
                "frame": {
                    "type": "control_request",
                    "request_id": "rx",
                    "request": {"subtype": "something_else"},
                },
            },
            {"do": "await_response", "request_id": "rx", "within": 20},
            {"do": "emit", "frame": _result(text="done")},
            {"do": "await_stdin_eof"},
        ],
    }
    result, prompt = await _wire_run(scenario, [])

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.asks == []
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    assert response["response"]["subtype"] == "error"


async def test_malformed_request_denied_without_asks(tmp_path: pathlib.Path) -> None:
    """A can_use_tool without an input object is denied, not asked."""
    record = tmp_path / "record.jsonl"
    scenario = {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "read_initialize"},
            {"do": "init"},
            {"do": "read_user"},
            {
                "do": "emit",
                "frame": {
                    "type": "control_request",
                    "request_id": "r1",
                    "request": {"subtype": "can_use_tool", "tool_name": "Write"},
                },
            },
            {"do": "await_response", "request_id": "r1", "within": 20},
            {"do": "emit", "frame": _result(text="done")},
            {"do": "await_stdin_eof"},
        ],
    }
    result, prompt = await _wire_run(scenario, [])

    assert result.status is AgentRunStatus.SETTLED
    assert prompt.asks == []
    records = _records(record)
    response = next(entry["response"] for entry in records if "response" in entry)
    assert response["response"]["response"]["behavior"] == "deny"


class _GatedPrompt(FakeUserPrompt):
    """An ask that hangs until cancelled, recording the cancellation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cancelled: list[str] = []

    async def _hang(self, message: str) -> Any:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.append(message)
            raise
        raise AssertionError("the hanging ask should never be answered")

    async def ask_choice(self, message: str, options: list[str], **kwargs: Any) -> Any:
        _ = await super().ask_choice(message, options, **kwargs)
        return await self._hang(message)

    async def ask_text(self, message: str, **kwargs: Any) -> Any:
        _ = await super().ask_text(message, **kwargs)
        return await self._hang(message)

    async def ask_multi_choice(
        self, message: str, options: list[str], **kwargs: Any
    ) -> Any:
        _ = await super().ask_multi_choice(message, options, **kwargs)
        return await self._hang(message)


def _capable_gated() -> _GatedPrompt:
    return _GatedPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )


async def test_unavailable_at_once_refuses_with_session(tmp_path: pathlib.Path) -> None:
    """Nobody-to-ask ends the run as refused, naming the question (D1.unavailable)."""
    record = tmp_path / "record.jsonl"
    result, prompt = await _wire_run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {"do": "await_response", "request_id": "r1", "within": 20},
                {"do": "await_stdin_eof"},
            ],
        },
        [ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)],
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "Which codename should I use?" in result.error
    assert result.session is not None
    assert result.session.session_id == "11111111-2222-3333-4444-555555555555"
    assert prompt.availability_checks == 1
    records = _records(record)
    assert {"no_response": "r1"} in records
    assert not [entry for entry in records if "response" in entry]


async def test_task_cancellation_aborts_without_result(tmp_path: pathlib.Path) -> None:
    """Withdrawing the run aborts claude and cancels the pending ask (D1.cancel)."""
    record = tmp_path / "record.jsonl"
    prompt = _capable_gated()
    scenario = {
        "record_path": str(record),
        "record_driver": True,
        "steps": [
            {"do": "read_initialize"},
            {"do": "init"},
            {"do": "read_user"},
            {"do": "spawn_child"},
            {
                "do": "request",
                "request_id": "r1",
                "tool_name": "AskUserQuestion",
                "input": _wire_ask_input(),
                "tool_use_id": "toolu_1",
            },
            {"do": "await_stdin_eof"},
        ],
    }
    config = {"may_ask_questions": True}
    task = asyncio.get_running_loop().create_task(
        _run(
            scenario,
            handler_config=config,
            service_overrides={IUserPrompt: prompt},
        )
    )
    deadline = time.monotonic() + 10
    while not prompt.asks and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert prompt.asks, "the ask never reached the person"
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert prompt.cancelled == ["Codename: Which codename should I use?"]
    pids = next(entry["pids"] for entry in _records(record) if "pids" in entry)
    assert await _wait_gone(pids["agent"])
    assert await _wait_gone(pids["child"])


async def test_action_run_cancelled_from_ask_aborts_without_result(
    tmp_path: pathlib.Path,
) -> None:
    """An ask that reports the run torn down aborts claude and re-raises."""
    from finecode_extension_api.interfaces.iprojectactionrunner import (
        ActionRunCancelled,
    )

    record = tmp_path / "record.jsonl"

    class _CancellingPrompt(FakeUserPrompt):
        async def ask_choice(self, *args: Any, **kwargs: Any) -> Any:
            raise ActionRunCancelled("torn down")

    prompt = _CancellingPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )
    with pytest.raises(ActionRunCancelled):
        await _run(
            {
                "record_path": str(record),
                "record_driver": True,
                "steps": [
                    {"do": "read_initialize"},
                    {"do": "init"},
                    {"do": "read_user"},
                    {"do": "spawn_child"},
                    {
                        "do": "request",
                        "request_id": "r1",
                        "tool_name": "AskUserQuestion",
                        "input": _wire_ask_input(),
                        "tool_use_id": "toolu_1",
                    },
                    {"do": "await_stdin_eof"},
                ],
            },
            handler_config={"may_ask_questions": True},
            service_overrides={IUserPrompt: prompt},
        )
    pids = next(entry["pids"] for entry in _records(record) if "pids" in entry)
    assert await _wait_gone(pids["agent"])
    assert await _wait_gone(pids["child"])


async def test_exit_while_pending_is_failed_and_bounded(
    tmp_path: pathlib.Path,
) -> None:
    """A claude that dies mid-question fails without waiting out the deadline."""
    record = tmp_path / "record.jsonl"
    prompt = _capable_gated()
    result = await asyncio.wait_for(
        _run(
            {
                "record_path": str(record),
                "record_driver": True,
                "steps": [
                    {"do": "read_initialize"},
                    {"do": "init"},
                    {"do": "read_user"},
                    {
                        "do": "request",
                        "request_id": "r1",
                        "tool_name": "AskUserQuestion",
                        "input": _wire_ask_input(),
                        "tool_use_id": "toolu_1",
                    },
                    {"do": "exit", "code": 0},
                ],
            },
            handler_config={"may_ask_questions": True},
            service_overrides={IUserPrompt: prompt},
        ),
        timeout=10,
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "claude stopped without reporting a result"
    assert prompt.cancelled == ["Codename: Which codename should I use?"]


async def test_result_while_pending_settles_and_cancels_ask(
    tmp_path: pathlib.Path,
) -> None:
    """A result that arrives first wins; the pending ask is withdrawn (D1.end)."""
    record = tmp_path / "record.jsonl"
    marker = tmp_path / "asked"
    prompt = _capable_gated()
    original_hang = _GatedPrompt._hang

    async def _touch_and_hang(self: _GatedPrompt, message: str) -> Any:
        _ = marker.write_text("asked", encoding="utf-8")
        return await original_hang(self, message)

    prompt._hang = _touch_and_hang.__get__(prompt, _GatedPrompt)
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {"do": "wait_for", "path": str(marker), "timeout": 20},
                {"do": "emit", "frame": _result(text="late answer")},
                {"do": "await_stdin_eof"},
            ],
        },
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "late answer"
    assert prompt.cancelled == ["Codename: Which codename should I use?"]
    records = _records(record)
    assert not [entry for entry in records if "response" in entry]


async def test_cancel_request_withdraws_the_group_entry(tmp_path: pathlib.Path) -> None:
    """RT1: a withdrawn group entry is never asked and never answered."""
    record = tmp_path / "record.jsonl"
    marker = tmp_path / "entry2"
    tool_input: dict[str, Any] = {
        "questions": [
            {"question": "Q1?", "options": [{"label": "A"}]},
            {"question": "Q2?", "options": [{"label": "B"}]},
            {"question": "Q3?", "options": [{"label": "C"}]},
        ]
    }
    scripted = _GatedPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )
    answers: list[Any] = [
        ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="A"),
    ]
    calls: list[str] = []
    cancelled: list[str] = []
    real_choice = FakeUserPrompt.ask_choice

    async def _scripted_choice(
        self: Any, message: str, options: Any, **kwargs: Any
    ) -> Any:
        calls.append(message)
        if len(calls) == 2:
            _ = marker.write_text("asked", encoding="utf-8")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(message)
                raise
            raise AssertionError("entry 2 must not be answered")
        return await real_choice(self, message, options, **kwargs)

    async def _never_text(self: Any, _message: str, **kwargs: Any) -> Any:
        raise AssertionError("entry 3 must never be asked")

    scripted.ask_choice = _scripted_choice.__get__(scripted, _GatedPrompt)
    scripted.ask_text = _never_text.__get__(scripted, _GatedPrompt)
    scripted.answers.extend(answers)
    scripted.answers.append(
        ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Allow once")
    )
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": tool_input,
                    "tool_use_id": "toolu_1",
                },
                {"do": "wait_for", "path": str(marker), "timeout": 20},
                {"do": "cancel_request", "request_id": "r1"},
                {
                    "do": "request",
                    "request_id": "r2",
                    "tool_name": "Write",
                    "input": {"file_path": "x"},
                    "tool_use_id": "toolu_2",
                },
                {"do": "await_response", "request_id": "r2", "within": 20},
                {"do": "emit", "frame": _result(text="done")},
                {"do": "await_stdin_eof"},
            ],
        },
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: scripted},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert calls[:2] == ["Question 1 of 3. Q1?", "Question 2 of 3. Q2?"]
    assert cancelled == ["Question 2 of 3. Q2?"]
    assert len(calls) == 3
    assert calls[2].startswith("Claude Code asks to use Write.")
    records = _records(record)
    responses = [entry["response"] for entry in records if "response" in entry]
    assert [response["response"]["request_id"] for response in responses] == ["r2"]
    assert responses[0]["response"]["response"]["behavior"] == "allow"


async def test_concurrent_requests_are_serialised(tmp_path: pathlib.Path) -> None:
    """Two requests at once: the first UNAVAILABLE ends the run, the second unasked."""
    record = tmp_path / "record.jsonl"
    result, prompt = await _wire_run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {
                    "do": "request",
                    "request_id": "r2",
                    "tool_name": "Write",
                    "input": {"file_path": "y"},
                    "tool_use_id": "toolu_2",
                },
                {"do": "await_stdin_eof"},
            ],
        },
        [ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)],
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "Which codename should I use?" in result.error
    assert len(prompt.asks) == 1
    records = _records(record)
    assert not [entry for entry in records if "response" in entry]


async def test_exit_and_unavailable_together_is_an_exit(
    tmp_path: pathlib.Path,
) -> None:
    """When claude is already gone, the exit status wins over UNAVAILABLE (D1.exit)."""
    record = tmp_path / "record.jsonl"
    marker = tmp_path / "touch"

    class _SlowUnavailable(FakeUserPrompt):
        async def ask_choice(self, *args: Any, **kwargs: Any) -> Any:
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.0)
            return ElicitationResult(outcome=ElicitationOutcome.UNAVAILABLE)

    prompt = _SlowUnavailable(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {"do": "touch", "path": str(marker)},
                {"do": "exit", "code": 0},
            ],
        },
        handler_config={"may_ask_questions": True},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "claude stopped without reporting a result"


async def test_exit_after_decided_and_unrouted_denials_is_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Without a result frame there is nothing to amend: exits fail."""
    from finecode_extension_api.interfaces.iuserprompt import PromptAvailability

    cases: list[tuple[str, dict[str, Any], list[Any]]] = [
        (
            "decided",
            {"may_ask_questions": True},
            [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Deny")],
        ),
        (
            "unrouted",
            {"may_ask_questions": True, "disallowed_tools": ["Write"]},
            [],
        ),
    ]
    for name, config, answers in cases:
        record = tmp_path / f"{name}.jsonl"
        prompt = FakeUserPrompt(
            answers=list(answers),
            availability=PromptAvailability(
                can_ask=True, can_ask_text=True, can_ask_multi_choice=True
            ),
        )
        result = await asyncio.wait_for(
            _run(
                {
                    "record_path": str(record),
                    "record_driver": True,
                    "steps": [
                        {"do": "read_initialize"},
                        {"do": "init"},
                        {"do": "read_user"},
                        {
                            "do": "request",
                            "request_id": "r1",
                            "tool_name": "Write",
                            "input": {"file_path": "x"},
                            "tool_use_id": "toolu_1",
                        },
                        {"do": "exit", "code": 0},
                    ],
                },
                handler_config=config,
                service_overrides={IUserPrompt: prompt},
            ),
            timeout=10,
        )

        assert result.status is AgentRunStatus.FAILED
        assert result.error == "claude stopped without reporting a result"


async def test_continuation_in_question_mode_settles(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A D1.unavailable session can be continued by a question-mode run (D1.session)."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project_dir = _project_dir(tmp_path)
    session_id = "33333333-3333-4333-8333-333333333333"

    async def _question_run(
        scenario: dict[str, Any],
        answers: list[Any],
        prompt_text: str = "work",
        continue_session: Any = None,
    ) -> Any:
        prompt = FakeUserPrompt(
            answers=answers,
            availability=PromptAvailability(
                can_ask=True, can_ask_text=True, can_ask_multi_choice=True
            ),
        )
        return await _run(
            scenario,
            prompt=prompt_text,
            project_dir=project_dir,
            handler_config=dict(_CONTINUE_CONFIG, may_ask_questions=True),
            continue_session=continue_session,
            service_overrides={IUserPrompt: prompt},
        ), prompt

    first_record = tmp_path / "first.jsonl"
    first, _ = await _question_run(
        {
            "session_id": session_id,
            "config_dir": str(tmp_path),
            "write_native": True,
            "record_path": str(first_record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init", "session_id": session_id},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {"do": "await_response", "request_id": "r1", "within": 20},
                {
                    "do": "emit",
                    "frame": _result(text="first", session_id=session_id),
                },
                {"do": "await_stdin_eof"},
            ],
        },
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Quokka-7")],
        prompt_text="first",
    )
    assert first.status is AgentRunStatus.SETTLED
    assert first.session is not None
    assert first.session.session_id == session_id

    second_record = tmp_path / "second.jsonl"
    second, prompt2 = await _question_run(
        {
            "config_dir": str(tmp_path),
            "record_path": str(second_record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init", "session_id": session_id},
                {"do": "read_user"},
                {
                    "do": "request",
                    "request_id": "r2",
                    "tool_name": "Write",
                    "input": {"file_path": "z"},
                    "tool_use_id": "toolu_2",
                },
                {"do": "await_response", "request_id": "r2", "within": 20},
                {
                    "do": "emit",
                    "frame": _result(text="second", session_id=session_id),
                },
                {"do": "await_stdin_eof"},
            ],
        },
        [ElicitationResult(outcome=ElicitationOutcome.ANSWERED, value="Allow once")],
        prompt_text="second",
        continue_session=first.session,
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    assert len(prompt2.asks) == 1
    records = _records(second_record)
    argv = next(entry["argv"] for entry in records if "argv" in entry)
    assert "--resume" in argv
    assert "--permission-prompt-tool" in argv


def _assistant_tool_use(tool_use_id: str, name: str = "Write") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}
            ],
        },
    }


async def test_settle_budget_pauses_while_blocked(tmp_path: pathlib.Path) -> None:
    """A 1 s budget survives a 2 s answer while claude waits on the host (D2.G1)."""
    record = tmp_path / "record.jsonl"

    async def _slow_allow(
        self: Any, _message: str, _options: Any, **kwargs: Any
    ) -> Any:
        await asyncio.sleep(2.0)
        return ElicitationResult(
            outcome=ElicitationOutcome.ANSWERED, value="Allow once"
        )

    prompt = FakeUserPrompt(
        availability=PromptAvailability(
            can_ask=True, can_ask_text=True, can_ask_multi_choice=True
        )
    )
    prompt.ask_choice = _slow_allow.__get__(prompt, FakeUserPrompt)
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {"do": "emit", "frame": _assistant_tool_use("toolu_9")},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "Write",
                    "input": {"file_path": "x"},
                    "tool_use_id": "toolu_9",
                },
                {"do": "await_response", "request_id": "r1", "within": 20},
                {"do": "emit", "frame": _result(text="done")},
                {"do": "await_stdin_eof"},
            ],
        },
        handler_config={"may_ask_questions": True, "settle_timeout_sec": 1.0},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "done"


async def test_sibling_tool_use_keeps_budget_ticking(tmp_path: pathlib.Path) -> None:
    """An unanswered sibling call means claude is not blocked on the host (D1.G6)."""
    record = tmp_path / "record.jsonl"
    prompt = _capable_gated()
    result = await _run(
        {
            "record_path": str(record),
            "record_driver": True,
            "steps": [
                {"do": "read_initialize"},
                {"do": "init"},
                {"do": "read_user"},
                {"do": "emit", "frame": _assistant_tool_use("toolu_1")},
                {"do": "emit", "frame": _assistant_tool_use("toolu_sib", "Bash")},
                {
                    "do": "request",
                    "request_id": "r1",
                    "tool_name": "AskUserQuestion",
                    "input": _wire_ask_input(),
                    "tool_use_id": "toolu_1",
                },
                {"do": "await_stdin_eof"},
            ],
        },
        handler_config={"may_ask_questions": True, "settle_timeout_sec": 1.0},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "settle budget" in result.error
    assert "Which codename should I use?" in result.error
    assert result.session is not None
    assert prompt.cancelled == ["Codename: Which codename should I use?"]
    records = _records(record)
    assert not [entry for entry in records if "response" in entry]


async def test_request_without_tool_use_id_counts_for_budget(
    tmp_path: pathlib.Path,
) -> None:
    """A routed request without an id never pauses, but still ends the budget."""
    record = tmp_path / "record.jsonl"
    prompt = _capable_gated()
    result = await _run(
        _wire_scenario(
            record,
            "r1",
            "Write",
            {"file_path": "x"},
            tool_use_id=None,
        ),
        handler_config={"may_ask_questions": True, "settle_timeout_sec": 1.0},
        service_overrides={IUserPrompt: prompt},
    )

    assert result.status is AgentRunStatus.REFUSED_INTERACTION
    assert result.error is not None
    assert "settle budget" in result.error
