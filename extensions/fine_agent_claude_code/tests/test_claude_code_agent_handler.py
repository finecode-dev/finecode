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
import json
import os
import pathlib
import sys
import time
from typing import Any

import pytest
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from finecode_extension_api.interfaces import icommandrunner
from finecode_extension_runner.testing import run_handler

from fine_agent_claude_code import claude_code_agent_handler
from fine_agent_claude_code.claude_code_agent_handler import ClaudeCodeAgentHandler

_FAKE_CLAUDE = pathlib.Path(__file__).parent / "fake_claude.py"


_ACTIVE_SCENARIO: dict[str, Any] = {}


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
) -> RunAgentTaskRunResult:
    global _ACTIVE_SCENARIO
    _ACTIVE_SCENARIO = scenario
    try:
        result = await run_handler(
            _FakeClaudeHandler,
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


async def test_profile_selects_its_model(tmp_path: pathlib.Path) -> None:
    """A profile is the only way one task runs on a different model than
    another, so its model must reach the command."""
    record = tmp_path / "record.jsonl"
    await _run(
        _settled_scenario(record),
        handler_config={"model": "M1", "profiles": {"p": {"model": "M2"}}},
        profile="p",
    )

    assert _flag_value(_recorded_argv(record), "--model") == "M2"


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


async def test_structured_output_is_returned() -> None:
    result = await _run(
        _schema_scenario(structured_output={"answer": "42"}),
        output_schema=_SCHEMA,
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"answer": "42"}


async def test_settling_without_structured_output_is_a_failure() -> None:
    """A settled run with no structured output must not hand the caller `None`
    where it asked for a report."""
    result = await _run(_schema_scenario(), output_schema=_SCHEMA)

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "claude settled without structured output"
    assert result.output == "done"


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
        **{"model": None, "settle_timeout_sec": 900.0} | values
    )


def _cmd(**config: Any) -> list[str]:
    """The real builder's argv for a config plus a resolved settings object.

    A profile moves `model` out of the config, so it travels through settings
    the way a run does.
    """
    config = dict(config)
    settings = _settings(model=config.pop("model", None))
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
