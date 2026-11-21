"""Configuration tests for the Codex agent handler.

Profile resolution happens before any process is spawned, so a typo in a
profile name or a bad timeout must fail fast with an explained result and no
child process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import pathlib
import sys
import time
import typing
import unittest.mock
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fine_agent.history_types import (
    AgentBackend,
    HistoryDiscoveryStatus,
    NativeHistoryRole,
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
from fine_agent.snapshot_stream import SnapshotStream
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import icommandrunner, iuser_messenger
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner import partial_result_sender
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session, run_handler

import fine_agent_codex
from fine_agent_codex import codex_agent_handler
from fine_agent_codex.codex_agent_handler import (
    CodexAgentHandler,
    CodexAgentHandlerConfig,
    CodexAgentProfile,
    CodexSandboxMode,
)
from fine_agent_codex.run_codex_agent_task_action import RunCodexAgentTaskAction
from tests.fake_codex_support import DriverRecord, FakeScenario


def _handler_with(**config: Any) -> CodexAgentHandler:
    handler = CodexAgentHandler.__new__(CodexAgentHandler)
    defaults: dict[str, Any] = {
        "model": None,
        "thinking": None,
        "sandbox_mode": CodexSandboxMode.READ_ONLY,
        "skip_git_repo_check": False,
        "settle_timeout_sec": 900.0,
        "profiles": {},
    }
    handler.config = type("_Config", (), defaults | config)()
    return handler


def test_top_level_settings_resolve_without_a_profile() -> None:
    settings = _handler_with(model="m1", settle_timeout_sec=11.0)._resolve(None)
    assert not isinstance(settings, str)
    assert settings.model == "m1"
    assert settings.settle_timeout_sec == 11.0


def test_profile_overrides_only_the_fields_it_sets() -> None:
    handler = _handler_with(
        model="m1",
        settle_timeout_sec=900.0,
        profiles={"p": CodexAgentProfile(model="m2")},
    )
    settings = handler._resolve("p")
    assert not isinstance(settings, str)
    assert settings.model == "m2"
    assert settings.settle_timeout_sec == 900.0


def test_full_profile_override() -> None:
    handler = _handler_with(
        model="m1",
        settle_timeout_sec=900.0,
        profiles={"p": CodexAgentProfile(model="m2", settle_timeout_sec=5.0)},
    )
    settings = handler._resolve("p")
    assert not isinstance(settings, str)
    assert settings.model == "m2"
    assert settings.settle_timeout_sec == 5.0


def test_empty_profile_name_resolves() -> None:
    """An explicitly configured empty name is a valid key, not a missing one."""
    handler = _handler_with(model="m1", profiles={"": CodexAgentProfile(model="m2")})
    settings = handler._resolve("")
    assert not isinstance(settings, str)
    assert settings.model == "m2"


def test_profile_thinking_overrides_only_when_set() -> None:
    handler = _handler_with(
        thinking="medium",
        profiles={
            "set": CodexAgentProfile(thinking="high"),
            "inherit": CodexAgentProfile(model="m2"),
        },
    )
    top = handler._resolve(None)
    overridden = handler._resolve("set")
    inherited = handler._resolve("inherit")
    assert not isinstance(top, str)
    assert not isinstance(overridden, str)
    assert not isinstance(inherited, str)
    assert top.thinking == "medium"
    assert overridden.thinking == "high"
    assert inherited.thinking == "medium"


def test_build_command_passes_thinking_as_reasoning_effort_config() -> None:
    settings = codex_agent_handler._CodexRunSettings(
        model="m", thinking="high", settle_timeout_sec=900.0
    )
    argv = _handler_with()._build_command(settings, pathlib.Path("/tmp/a.txt"), None)
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="high"'


def test_build_command_omits_thinking_config_when_unset() -> None:
    settings = codex_agent_handler._CodexRunSettings(
        model="m", thinking=None, settle_timeout_sec=900.0
    )
    argv = _handler_with()._build_command(settings, pathlib.Path("/tmp/a.txt"), None)
    assert "--config" not in argv


def test_continuation_command_carries_thinking_after_resume() -> None:
    ref = AgentSessionRef(
        backend=AgentBackend.CODEX,
        session_id=MAIN_THREAD_ID,
        project_dir=ResourceUri("file:///ws/proj"),
    )
    settings = codex_agent_handler._CodexRunSettings(
        model="m", thinking="xhigh", settle_timeout_sec=900.0
    )
    argv = _handler_with()._build_command(
        settings, pathlib.Path("/tmp/a.txt"), None, continue_session=ref
    )
    assert argv.index("--config") > argv.index("resume")
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="xhigh"'


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.inf, math.nan])
def test_non_positive_or_non_finite_timeout_is_a_config_error(
    timeout: float,
) -> None:
    error = _handler_with(settle_timeout_sec=timeout)._resolve(None)
    assert isinstance(error, str)
    assert "settle_timeout_sec" in error


class _RecordingCommandRunner:
    def __init__(self) -> None:
        self.run_calls = 0
        self.start_long_running_calls = 0
        self.run_seen: list[
            tuple[list[str], pathlib.Path | None, dict[str, str] | None, bool]
        ] = []

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.run_calls += 1
        self.run_seen.append((list(cmd), cwd, env, new_process_group))
        raise AssertionError("agent processes must start through start_long_running")

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        self.start_long_running_calls += 1
        self.run_seen.append((list(cmd), cwd, env, new_process_group))
        raise AssertionError("an unknown profile must not spawn a process")

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        self.run_seen.append((list(cmd), cwd, env, False))
        raise NotImplementedError


async def test_unknown_profile_fails_before_spawning() -> None:
    """A typo in a profile name must name the profile and the ones that exist,
    without paying for a model run to discover it."""
    runner = _RecordingCommandRunner()
    result = await run_handler(
        CodexAgentHandler,
        RunAgentTaskRunPayload(prompt="hi", profile="nope"),
        action_cls=RunAgentTaskAction,
        handler_config={"profiles": {"b": {}, "a": {}}},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )
    assert isinstance(result, RunAgentTaskRunResult)
    assert result.status is AgentRunStatus.FAILED
    assert result.error == "unknown agent profile 'nope'; configured profiles: a, b"
    assert result.duration_sec == 0.0
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


def test_build_command_has_fixed_policy_flags_and_stdin_prompt() -> None:
    settings = codex_agent_handler._CodexRunSettings(
        model=None, thinking=None, settle_timeout_sec=900.0
    )
    answer = pathlib.Path("/tmp/answer.txt")
    argv = _handler_with()._build_command(settings, answer, None)
    assert argv[:9] == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
    ]
    assert "--output-last-message" in argv
    assert argv[argv.index("--output-last-message") + 1] == str(answer)
    assert argv[-1] == "-"
    assert "--profile" not in argv
    assert "--model" not in argv


def test_build_command_carries_model_git_override_and_schema() -> None:
    settings = codex_agent_handler._CodexRunSettings(
        model="gpt-5", thinking=None, settle_timeout_sec=900.0
    )
    answer = pathlib.Path("/tmp/answer.txt")
    schema = pathlib.Path("/tmp/schema.json")
    argv = _handler_with(skip_git_repo_check=True)._build_command(
        settings, answer, schema
    )
    assert argv[argv.index("--model") + 1] == "gpt-5"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--output-schema") + 1] == str(schema)
    assert argv[-1] == "-"


def test_sandbox_stays_top_level() -> None:
    """A profile can change the model and timeout but never widen the sandbox."""
    handler = _handler_with(
        sandbox_mode=CodexSandboxMode.READ_ONLY,
        profiles={"p": CodexAgentProfile(model="m2")},
    )
    settings = handler._resolve("p")
    assert not isinstance(settings, str)
    argv = handler._build_command(settings, pathlib.Path("/tmp/a.txt"), None)
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_executable_is_codex() -> None:
    assert _handler_with()._executable() == ["codex"]


_FAKE_CODEX = pathlib.Path(__file__).parent / "fake_codex.py"

_ACTIVE: dict[str, Any] = {
    "scenario": FakeScenario.SUCCESS,
    "record": "",
    "answer": "done",
}


class _FakeCodexHandler(CodexAgentHandler):
    def _executable(self) -> list[str]:
        return [
            sys.executable,
            str(_FAKE_CODEX),
            _ACTIVE["scenario"].value,
            _ACTIVE["record"],
            json.dumps(_ACTIVE["answer"]),
        ]


MAIN_THREAD_ID = "11111111-2222-3333-8444-555555555555"


@pytest.fixture(autouse=True)
def _isolated_codex_home(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native rollouts stay inside the test's tmp dir, never the dev home."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def _main_rollout(codex_home: pathlib.Path, thread_id: str) -> pathlib.Path:
    """The one rollout whose session-meta id is *thread_id*."""
    matches: list[pathlib.Path] = []
    for path in (codex_home / "sessions").rglob("rollout-*.jsonl"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                break
            if not isinstance(entry, dict) or entry.get("type") != "session_meta":
                continue
            payload = entry.get("payload")
            if isinstance(payload, dict) and payload.get("id") == thread_id:
                matches.append(path)
            break
    assert len(matches) == 1
    return matches[0]


def _native_files(codex_home: pathlib.Path) -> set[str]:
    base = codex_home / "sessions"
    if not base.is_dir():
        return set()
    return {str(path.relative_to(base)) for path in base.rglob("*") if path.is_file()}


async def _run(
    scenario: FakeScenario,
    *,
    prompt: str = "do the thing",
    answer: Any = "done",
    handler_config: dict[str, Any] | None = None,
    profile: str | None = None,
    output_schema: dict[str, Any] | None = None,
    service_overrides: dict[Any, Any] | None = None,
    record_path: pathlib.Path | None = None,
    project_dir: pathlib.Path | None = None,
    capture_history: bool = False,
    continue_session: AgentSessionRef | None = None,
    action_cls: Any = RunAgentTaskAction,
) -> RunAgentTaskRunResult:
    _ACTIVE["scenario"] = scenario
    _ACTIVE["record"] = str(record_path) if record_path is not None else ""
    _ACTIVE["answer"] = answer
    try:
        result = await run_handler(
            _FakeCodexHandler,
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
        _ACTIVE["scenario"] = FakeScenario.SUCCESS
        _ACTIVE["record"] = ""
        _ACTIVE["answer"] = "done"
    assert isinstance(result, RunAgentTaskRunResult)
    return result


def _read_record(path: pathlib.Path) -> DriverRecord:
    raw: DriverRecord = json.loads(path.read_text(encoding="utf-8"))
    return raw


def _assert_accounted(result: RunAgentTaskRunResult) -> None:
    assert result.duration_sec is not None and result.duration_sec >= 0.0
    if result.status is not AgentRunStatus.SETTLED:
        assert result.error


class _SpawnMethodRunner:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.run_calls = 0
        self.run_seen: list[
            tuple[list[str], pathlib.Path | None, dict[str, str] | None, bool]
        ] = []
        self.start_calls: list[tuple[list[str], pathlib.Path | None, bool]] = []

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.run_calls += 1
        self.run_seen.append((list(cmd), cwd, env, new_process_group))
        raise AssertionError("agent processes must start through start_long_running")

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        icommandrunner.check_argv(cmd)
        self.start_calls.append((list(cmd), cwd, new_process_group))
        self.run_seen.append((list(cmd), cwd, env, new_process_group))
        if self._error is not None:
            raise self._error
        raise FileNotFoundError(2, "No such file or directory")

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        self.run_seen.append((list(cmd), cwd, env, False))
        raise NotImplementedError


class _RecordingMessenger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)


@pytest.mark.parametrize("action_cls", [RunAgentTaskAction, RunCodexAgentTaskAction])
async def test_argv_prompt_and_cwd_come_from_the_real_builder(
    tmp_path: pathlib.Path, action_cls: Any
) -> None:
    """The fake sees the production argv, the exact prompt on stdin, and the
    requested project directory -- and the prompt itself never appears in argv."""
    record = tmp_path / "record.json"
    prompt = "refactor héllo\nmulti-line 世界"
    result = await _run(
        FakeScenario.SUCCESS,
        prompt=prompt,
        answer="canonical",
        record_path=record,
        project_dir=tmp_path,
        action_cls=action_cls,
    )

    assert result.status is AgentRunStatus.SETTLED
    driver = _read_record(record)
    assert driver["argv"][:8] == [
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
    ]
    assert driver["argv"][-1] == "-"
    assert "--profile" not in driver["argv"]
    assert "--model" not in driver["argv"]
    assert all(prompt not in element for element in driver["argv"])
    assert driver["prompt"] == prompt
    assert driver["cwd"] == str(tmp_path)
    _assert_accounted(result)


async def test_explicit_sandbox_choice_reaches_the_argv(
    tmp_path: pathlib.Path,
) -> None:
    """Writing tasks opt into a wider sandbox; the choice must be visible."""
    record = tmp_path / "record.json"
    await _run(
        FakeScenario.SUCCESS,
        record_path=record,
        handler_config={"sandbox_mode": "workspace-write"},
    )

    driver = _read_record(record)
    assert driver["argv"][driver["argv"].index("--sandbox") + 1] == ("workspace-write")


async def test_agent_spawns_through_start_long_running_in_a_new_group() -> None:
    """The agent run is long-lived, so it must not hold a bounded-job slot,
    and its teardown must reach the tools it spawns."""
    runner = _SpawnMethodRunner()
    result = await _run(
        FakeScenario.SUCCESS,
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "could not start" in (result.error or "")
    assert runner.run_calls == 0
    assert len(runner.start_calls) == 1
    assert runner.start_calls[0][2] is True
    _assert_accounted(result)


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        icommandrunner.UnsafeBatchArgumentError(
            "argument 1 cannot be passed safely to batch file codex.cmd"
        ),
        icommandrunner.UnlaunchableProgramError(
            "codex resolves to codex.js, which CreateProcess cannot launch"
        ),
    ],
)
async def test_an_unstartable_codex_is_a_structured_failure(error: Exception) -> None:
    """A missing binary or a refused shim is a `FAILED` result naming the
    program, never an exception out of the action."""

    result = await _run(
        FakeScenario.SUCCESS,
        service_overrides={icommandrunner.ICommandRunner: _SpawnMethodRunner(error)},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "codex" in (result.error or "")
    assert (getattr(error, "strerror", None) or str(error)) in (result.error or "")
    _assert_accounted(result)


async def test_invalid_timeout_fails_without_spawning() -> None:
    runner = _RecordingCommandRunner()
    result = await _run(
        FakeScenario.SUCCESS,
        handler_config={"settle_timeout_sec": 0},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "settle_timeout_sec" in (result.error or "")
    assert result.duration_sec == 0.0
    assert runner.start_long_running_calls == 0


async def test_canonical_answer_wins_over_streamed_text() -> None:
    """Intermediate agent messages are a failure fallback; the file the CLI
    wrote last is the answer a settled run reports."""
    result = await _run(FakeScenario.SUCCESS, answer="canonical final")

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "canonical final"
    assert result.error is None
    _assert_accounted(result)


def _discard_partial(
    _token: int | str,
    _result: code_action.RunActionResult,
    _formats: list[str] | None,
) -> None:
    return None


async def test_snapshots_use_current_agent_message_and_terminal_uses_answer_file(
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
    handler_source = f"{_FakeCodexHandler.__module__}.{_FakeCodexHandler.__qualname__}"
    actions = {
        RunAgentTaskAction.__name__: {
            "source": action_source,
            "handlers": [{"name": "codex", "source": handler_source}],
        }
    }
    _ACTIVE.update(
        scenario=FakeScenario.SUCCESS,
        record="",
        answer="canonical final",
    )
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
            plain_result = await session.run_action(
                RunAgentTaskAction.__name__, RunAgentTaskRunPayload(prompt="test")
            )
    finally:
        _ACTIVE.update(scenario=FakeScenario.SUCCESS, record="", answer="done")

    assert isinstance(token_result, RunAgentTaskRunResult)
    assert isinstance(plain_result, RunAgentTaskRunResult)
    assert dataclasses.replace(token_result, duration_sec=None, session=None) == (
        dataclasses.replace(plain_result, duration_sec=None, session=None)
    )
    assert token_result.output == "canonical final"
    assert token_events[-1] == token_result
    assert token_events[0].status is AgentRunStatus.RUNNING
    assert token_events[0].output == "streamed interim"
    assert token_events[0].usage is None and token_events[0].session is None


class _RecordingSender:
    def __init__(self) -> None:
        self.messages: list[str | None] = []
        self.begins: list[tuple[str, str | None, int | None, bool, int | None]] = []
        self.ends: list[str | None] = []
        self.percentages: list[int | None] = []

    async def begin(
        self,
        title: str,
        message: str | None = None,
        percentage: int | None = None,
        cancellable: bool = False,
        total: int | None = None,
    ) -> None:
        self.begins.append((title, message, percentage, cancellable, total))

    async def report(
        self,
        message: str | None = None,
        percentage: int | None = None,
    ) -> None:
        self.messages.append(message)
        self.percentages.append(percentage)

    async def end(self, message: str | None = None) -> None:
        self.ends.append(message)


class _RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.debugs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def exception(self, exception: Exception) -> None:
        self.errors.append(str(exception))

    def trace(self, message: str) -> None:
        self.debugs.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def disable(self, package: str) -> None:
        self.debugs.append(package)

    def enable(self, package: str) -> None:
        self.debugs.append(package)


class _StubProcess:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.prompt = ""

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    def write_to_stdin(self, value: str) -> None:
        self.prompt = value

    def close_stdin(self) -> None:
        return None


async def test_progress_names_work_not_text_and_thread_is_logged() -> None:
    """Progress carries fixed labels while commands, diffs and model text
    stay out of it; the thread id is logged for later inspection."""
    secret_command = "rm -rf /tmp/secret"
    secret_diff = "--- a/secret\n+++ b/secret"
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t-9"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "text": secret_command},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "file_change", "text": secret_diff},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "interim"},
            }
        ),
        json.dumps({"type": "error", "message": "a hiccup"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ]
    sender = _RecordingSender()
    logger = _RecordingLogger()
    handler = CodexAgentHandler(
        CodexAgentHandlerConfig(),
        logger,
        unittest.mock.MagicMock(),
        unittest.mock.MagicMock(),
        unittest.mock.MagicMock(),
    )
    state = codex_agent_handler._CodexRunState()
    stub = _StubProcess(lines)

    await handler._drive(
        typing.cast(icommandrunner.IAsyncProcess, stub),
        "prompt",
        code_action.ProgressContext(sender, "t"),
        state,
        SnapshotStream(
            typing.cast(code_action.PartialResultSender, unittest.mock.AsyncMock()),
            min_interval_sec=None,
            logger=handler.logger,
        ),
    )

    assert sender.messages == [
        "Agent turn started",
        "Running command",
        "Applying file changes",
        "Writing answer",
    ]
    for shown in sender.messages:
        assert shown is not None
        assert secret_command not in shown
        assert secret_diff not in shown
        assert "interim" not in shown
        assert "hiccup" not in shown
    assert any("t-9" in info for info in logger.infos)
    assert state.last_assistant_text == "interim"
    assert state.error == "a hiccup"
    assert state.usage is not None and state.usage.input_tokens == 1


async def test_noise_unknown_types_and_extra_keys_still_settle() -> None:
    """A CLI upgrade adding events or keys must not fail a run that is
    otherwise fine."""
    result = await _run(FakeScenario.NOISE, answer="fine")

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "fine"
    assert result.usage is not None
    assert result.usage.input_tokens == 12
    assert result.usage.cache_read_tokens == 34
    assert result.usage.output_tokens == 56
    assert result.usage.total_tokens is None
    _assert_accounted(result)


async def test_stream_without_a_terminal_is_not_a_finished_run() -> None:
    """The CLI died mid-run. Its text is kept, but reporting `SETTLED` would
    tell the caller a truncated run finished its work."""
    result = await _run(FakeScenario.NO_TERMINAL)

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "halfway"
    assert result.error == "codex stopped without reporting a result"
    _assert_accounted(result)


async def test_failed_terminal_fails_at_exit_zero_and_keeps_accounting() -> None:
    result = await _run(FakeScenario.FAILED)

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "boom"
    assert result.output == "partial work"
    assert result.usage is not None
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 6
    _assert_accounted(result)


async def test_success_terminal_with_nonzero_exit_fails_with_stderr() -> None:
    result = await _run(FakeScenario.NONZERO, answer="final")

    assert result.status is AgentRunStatus.FAILED
    assert result.error is not None
    assert "1" in result.error
    assert "auth expired" in result.error
    assert result.output == "before the crash"
    _assert_accounted(result)


async def test_diagnostic_then_completion_can_succeed() -> None:
    """A retry diagnostic and a failed tool do not condemn a run that went
    on to complete."""
    result = await _run(FakeScenario.RETRY, answer="recovered")

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "recovered"
    assert result.error is None
    _assert_accounted(result)


async def test_missing_answer_file_fails_and_keeps_fallback_text() -> None:
    result = await _run(FakeScenario.MISSING_ANSWER)

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "fallback text"
    assert result.error is not None and "answer" in result.error
    _assert_accounted(result)


async def test_empty_answer_settles_empty_with_a_warning() -> None:
    """An agent that settles without saying anything did its job; the empty
    output is diagnosable rather than a failure."""
    messenger = _RecordingMessenger()
    result = await _run(
        FakeScenario.EMPTY,
        service_overrides={iuser_messenger.IUserMessenger: messenger},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == ""
    assert len(messenger.warnings) == 1
    _assert_accounted(result)


async def test_explicit_zero_usage_is_reported_not_absent() -> None:
    """Zero is a figure the backend stood behind; `None` means nobody said."""
    result = await _run(FakeScenario.SUCCESS)

    assert result.usage is not None
    assert result.usage.input_tokens == 0
    assert result.usage.cache_read_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.cache_write_tokens is None
    assert result.usage.total_tokens is None
    assert result.usage.approx_cost_usd is None
    assert result.usage.provider is None
    assert result.usage.model is None
    assert result.turns is None
    _assert_accounted(result)


@pytest.mark.parametrize("action_cls", [RunAgentTaskAction, RunCodexAgentTaskAction])
async def test_structured_output_round_trips_through_a_real_schema_file(
    tmp_path: pathlib.Path, action_cls: Any
) -> None:
    """The schema the CLI validates against is the file the fake reads while
    running, not a copy of the test input."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    record = tmp_path / "record.json"
    result = await _run(
        FakeScenario.STRUCTURED_VALID,
        answer={"answer": "42"},
        output_schema=schema,
        record_path=record,
        action_cls=action_cls,
    )

    assert isinstance(result, RunAgentTaskRunResult)

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"answer": "42"}
    assert result.output == json.dumps({"answer": "42"})
    driver = _read_record(record)
    assert driver["schema"] == schema
    assert driver["schema_path"] is not None
    assert driver["schema_path"].endswith("schema.json")
    argv = driver["argv"]
    assert argv[argv.index("--output-schema") + 1] == driver["schema_path"]
    _assert_accounted(result)


async def test_no_schema_means_no_schema_flag_or_file(
    tmp_path: pathlib.Path,
) -> None:
    record = tmp_path / "record.json"
    result = await _run(FakeScenario.SUCCESS, record_path=record)

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output is None
    driver = _read_record(record)
    assert driver["schema"] is None
    assert driver["schema_path"] is None
    assert "--output-schema" not in driver["argv"]


@pytest.mark.parametrize("value", [False, 0, [], {}])
async def test_falsy_json_values_are_valid_structured_output(value: Any) -> None:
    """Only JSON null is absent; false, zero and empty collections are real
    answers a caller can structure."""
    result = await _run(
        FakeScenario.STRUCTURED_VALID,
        answer=value,
        output_schema={"type": ["boolean", "integer", "array", "object"]},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == value
    assert result.output == json.dumps(value)


async def test_malformed_json_answer_fails_and_keeps_raw_text() -> None:
    result = await _run(
        FakeScenario.STRUCTURED_INVALID,
        answer="not json{{{",
        output_schema={"type": "object"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "not json{{{"
    assert result.structured_output is None
    _assert_accounted(result)


async def test_json_null_answer_fails() -> None:
    """Null means the backend produced nothing, which is a failure when a
    schema was requested."""
    result = await _run(
        FakeScenario.STRUCTURED_INVALID,
        answer=None,
        output_schema={"type": "object"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "null"
    assert result.structured_output is None
    _assert_accounted(result)


async def test_missing_answer_with_schema_fails_and_keeps_fallback() -> None:
    result = await _run(
        FakeScenario.MISSING_ANSWER,
        output_schema={"type": "object"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "fallback text"
    assert result.structured_output is None
    _assert_accounted(result)


async def test_schema_mismatching_value_passes_through_for_the_caller() -> None:
    """The backend extracts JSON; type-checking it is the caller's job, so a
    syntactically valid value that misses the schema still settles."""
    result = await _run(
        FakeScenario.STRUCTURED_VALID,
        answer={"wrong": 1},
        output_schema={
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
        },
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"wrong": 1}


def test_package_root_exposes_the_agent_backend_alongside_the_installer() -> None:
    """Swapping backends is an import away: handler, config, profile and
    sandbox mode arrive with the installer exports they must not disturb."""
    assert fine_agent_codex.CodexAgentHandler is CodexAgentHandler
    assert fine_agent_codex.CodexAgentHandlerConfig is CodexAgentHandlerConfig
    assert fine_agent_codex.CodexAgentProfile is CodexAgentProfile
    assert fine_agent_codex.CodexSandboxMode is CodexSandboxMode
    assert fine_agent_codex.InstallCodexHandler is not None
    assert fine_agent_codex.InstallCodexHandlerConfig is not None


def _is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    return False


async def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _is_gone(pid)


def _wait_record(path: pathlib.Path, timeout: float = 15.0) -> DriverRecord:
    """The fake-only readiness poll: proves the test acted on a live owned
    process rather than on a spawn that never happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return _read_record(path)
            except ValueError:
                pass
        time.sleep(0.05)
    return _read_record(path)


async def test_a_wedged_run_is_torn_down_with_its_tree(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that outlives its timeout keeps write access to the project and
    keeps spending; the teardown has to reach the refusing agent and the
    child it spawned."""
    monkeypatch.setattr(codex_agent_handler, "_EXIT_GRACE_SEC", 0.3)
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)
    record = tmp_path / "record.json"
    result = await _run(
        FakeScenario.IGNORE_TERM,
        handler_config={"settle_timeout_sec": 0.5},
        record_path=record,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not finish within" in (result.error or "")
    driver = _read_record(record)
    assert await _wait_gone(driver["agent_pid"])
    assert driver["child_pid"] is not None
    assert await _wait_gone(driver["child_pid"])
    _assert_accounted(result)


async def test_cancel_reaches_a_live_owned_run_and_reraises(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withdrawing the request stops the agent; nothing is returned on this
    path, and whatever the run spent is lost with it."""
    monkeypatch.setattr(codex_agent_handler, "_EXIT_GRACE_SEC", 0.3)
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)
    record = tmp_path / "record.json"
    task = asyncio.create_task(
        _run(FakeScenario.HANG, record_path=record),
    )
    driver = await asyncio.to_thread(_wait_record, record)
    assert driver["agent_pid"] > 0
    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_gone(driver["agent_pid"])


async def test_undecodable_stream_propagates_after_cleanup(
    tmp_path: pathlib.Path,
) -> None:
    """A line the pipe cannot carry fails the stream, not the verdict logic;
    the failure propagates only once the group is gone."""
    record = tmp_path / "record.json"
    with pytest.raises(Exception, match="cannot be recovered"):
        await _run(FakeScenario.BAD_UTF8, record_path=record)
    driver = _read_record(record)
    assert await _wait_gone(driver["agent_pid"])


async def test_a_leader_that_exits_leaving_a_child_still_settles_clean(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown reaches the whole group, not just the process that was
    spawned: a settled run must not leave a child editing the project."""
    monkeypatch.setattr(codex_agent_handler, "_EXIT_GRACE_SEC", 0.3)
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)
    record = tmp_path / "record.json"
    result = await _run(FakeScenario.LEAVE_CHILD, answer="done", record_path=record)

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "done"
    driver = _read_record(record)
    assert driver["child_pid"] is not None
    assert await _wait_gone(driver["agent_pid"])
    assert await _wait_gone(driver["child_pid"])
    _assert_accounted(result)


def _assert_gone(*paths: str) -> None:
    for path in paths:
        assert not pathlib.Path(path).exists()


async def test_used_paths_are_gone_after_success_and_after_spawn_failure(
    tmp_path: pathlib.Path,
) -> None:
    """Schema and answer paths live only for the run: directory cleanup runs
    after process cleanup, and also when the process never started."""
    record = tmp_path / "record.json"
    result = await _run(
        FakeScenario.STRUCTURED_VALID,
        answer={"a": 1},
        output_schema={"type": "object"},
        record_path=record,
    )
    assert result.status is AgentRunStatus.SETTLED
    driver = _read_record(record)
    assert driver["schema_path"] is not None
    _assert_gone(driver["answer_path"], driver["schema_path"])

    created: list[str] = []
    real_tmpdir = codex_agent_handler.tempfile.TemporaryDirectory

    def _tracking_tmpdir(*args: Any, **kwargs: Any) -> Any:
        tmp = real_tmpdir(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    runner = _SpawnMethodRunner(FileNotFoundError(2, "No such file"))
    with unittest.mock.patch.object(
        codex_agent_handler.tempfile, "TemporaryDirectory", _tracking_tmpdir
    ):
        failed = await _run(
            FakeScenario.SUCCESS,
            service_overrides={icommandrunner.ICommandRunner: runner},
        )
    assert failed.status is AgentRunStatus.FAILED
    assert created
    for name in created:
        _assert_gone(name)


class _ScriptedProcess:
    """A process double with a scripted liveness ladder, for the teardown
    paths a fake that exits cleanly never exercises."""

    def __init__(self, lines: list[str], alive: list[bool]) -> None:
        self._lines = lines
        self._alive = list(alive)
        self.terminated = 0
        self.killed = 0
        self.last_input: str | None = None
        self.waits: list[float | None] = []

    async def stdout_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    def write_to_stdin(self, value: str) -> None:
        self.last_input = value

    def close_stdin(self) -> None:
        pass

    async def wait_for_end(self, timeout: float | None = None) -> None:
        self.waits.append(timeout)

    def get_exit_code(self) -> int | None:
        return 0

    def get_output(self) -> str:
        raise AssertionError("streaming forbids get_output")

    def get_error_output(self) -> str:
        return ""

    def is_alive(self) -> bool:
        if len(self._alive) > 1:
            return self._alive.pop(0)
        return self._alive[0]

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1


class _FailingStreamProcess(_ScriptedProcess):
    async def stdout_lines(self) -> AsyncIterator[str]:
        yield "not json"
        raise RuntimeError("broken pipe")


class _StubRunner:
    """Serves one scripted process, writing the answer file from the real
    argv so the finish path sees what the builder promised."""

    def __init__(self, process: _ScriptedProcess, answer: str) -> None:
        self._process = process
        self._answer = answer
        self.seen: list[
            tuple[list[str], pathlib.Path | None, dict[str, str] | None, bool]
        ] = []

    async def start_long_running(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.seen.append((list(cmd), cwd, env, new_process_group))
        argv = list(cmd)
        answer_path = argv[argv.index("--output-last-message") + 1]
        _ = await asyncio.to_thread(
            pathlib.Path(answer_path).write_text, self._answer, encoding="utf-8"
        )
        return self._process

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        self.seen.append((list(cmd), cwd, env, new_process_group))
        raise AssertionError("agent processes must start through start_long_running")

    def run_sync(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        self.seen.append((list(cmd), cwd, env, False))
        raise NotImplementedError


async def test_a_live_group_after_success_is_a_lifecycle_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled run whose teardown still observes a live group must not be
    reported as success; the agent tree is still out there."""
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.2)
    process = _ScriptedProcess(
        lines=[json.dumps({"type": "turn.completed"})],
        alive=[False, True, True, True],
    )
    result = await _run(
        FakeScenario.SUCCESS,
        answer="done",
        service_overrides={icommandrunner.ICommandRunner: _StubRunner(process, "done")},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "done"
    assert result.error is not None and "did not stop" in result.error
    assert process.terminated == 1
    assert process.killed == 1
    _assert_accounted(result)


async def test_teardown_problems_do_not_replace_a_prior_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the drive already failed, a group that will not die is logged,
    not reported in place of the original failure."""
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.2)
    process = _FailingStreamProcess(lines=[], alive=[True])
    with pytest.raises(Exception, match="broken pipe"):
        await _run(
            FakeScenario.SUCCESS,
            service_overrides={
                icommandrunner.ICommandRunner: _StubRunner(process, "done")
            },
        )
    assert process.terminated == 1
    assert process.killed == 1


async def test_uncaptured_has_no_observation() -> None:
    result = await _run(FakeScenario.SUCCESS)

    assert result.observation is None


async def test_captured_run_reports_available_history(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    result = await _run(FakeScenario.SUCCESS, capture_history=True)

    assert result.status is AgentRunStatus.SETTLED
    observation = result.observation
    assert observation is not None
    assert observation.backend is AgentBackend.CODEX
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert observation.session_id == MAIN_THREAD_ID
    assert observation.backend_version == "0.163.0"
    assert len(observation.files) == 3
    assert observation.usage_provenance.token_source is UsageSource.CODEX_TURN
    assert observation.usage_provenance.token_scope is UsageScope.UNKNOWN
    main = next(f for f in observation.files if f.role.value == "main")
    content = resource_uri_to_path(main.source_uri).read_text(encoding="utf-8")
    assert MAIN_THREAD_ID in content
    assert result.usage is not None
    assert result.usage.provider == "openai"
    assert result.usage.model == "gpt-test"
    assert result.turns == 2


def _session_ref(
    project_dir: pathlib.Path,
    session_id: str = MAIN_THREAD_ID,
    backend: AgentBackend = AgentBackend.CODEX,
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


async def test_settled_run_reports_session_and_continuations_reuse_it(
    tmp_path: pathlib.Path,
) -> None:
    """A new settled run hands out its native session; later prompts resume it.

    A second prompt must reopen the same native session (not start one), and
    a third can reuse the reference the second run returned. Decoys and the
    session-file set are untouched by the resumes.
    """
    project_dir = _project_dir(tmp_path)
    home = pathlib.Path(os.environ["CODEX_HOME"])
    first = await _run(
        FakeScenario.SUCCESS,
        prompt="first",
        answer="one",
        project_dir=project_dir,
        handler_config={"model": "m"},
    )

    assert first.status is AgentRunStatus.SETTLED
    assert first.session == _session_ref(project_dir)
    assert first.session is not None and first.session.backend is AgentBackend.CODEX
    main_file = _main_rollout(home, MAIN_THREAD_ID)
    assert "first" in main_file.read_text(encoding="utf-8")
    files_before = _native_files(home)
    assert files_before
    decoy = next(
        path
        for path in (home / "sessions").rglob("rollout-*.jsonl")
        if "other-1" in path.read_text(encoding="utf-8")
    )
    decoy_before = decoy.read_bytes()

    second = await _run(
        FakeScenario.SUCCESS,
        prompt="second",
        answer="two",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert second.status is AgentRunStatus.SETTLED
    assert second.session == first.session
    resumed = main_file.read_text(encoding="utf-8")
    assert "first" in resumed and "second" in resumed
    assert _native_files(home) == files_before
    assert decoy.read_bytes() == decoy_before

    third = await _run(
        FakeScenario.SUCCESS,
        prompt="third",
        answer="three",
        project_dir=project_dir,
        continue_session=second.session,
        handler_config={"model": "m"},
    )

    assert third.status is AgentRunStatus.SETTLED
    assert third.session == first.session
    assert "third" in main_file.read_text(encoding="utf-8")
    assert _native_files(home) == files_before


async def test_continuation_argv_resumes_with_current_settings(
    tmp_path: pathlib.Path,
) -> None:
    """The resume argv carries the current model and sandbox, not saved ones."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        handler_config={"model": "old-model"},
    )
    assert first.session is not None
    record = tmp_path / "resume-record.json"
    second = await _run(
        FakeScenario.SUCCESS,
        prompt="again",
        project_dir=project_dir,
        continue_session=first.session,
        record_path=record,
        handler_config={"model": "new-model", "sandbox_mode": "workspace-write"},
    )

    assert second.status is AgentRunStatus.SETTLED
    driver = _read_record(record)
    argv = driver["argv"]
    assert argv[:9] == [
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "workspace-write",
        "--color",
        "never",
        "resume",
        "--json",
    ]
    assert argv[9:13] == [
        "--output-last-message",
        driver["answer_path"],
        "--model",
        "new-model",
    ]
    assert argv[-2:] == [MAIN_THREAD_ID, "-"]
    assert "--last" not in argv
    assert "--fork" not in argv
    assert "--ephemeral" not in argv
    assert not any(element.startswith("thread-") for element in argv)
    assert driver["prompt"] == "again"


def test_continuation_build_command_form() -> None:
    """Resume flags belong to `resume`; the thread is addressed by full UUID."""
    ref = AgentSessionRef(
        backend=AgentBackend.CODEX,
        session_id=MAIN_THREAD_ID,
        project_dir=ResourceUri("file:///ws/proj"),
    )
    settings = codex_agent_handler._CodexRunSettings(
        model="gpt-5", thinking=None, settle_timeout_sec=900.0
    )
    answer = pathlib.Path("/tmp/answer.txt")
    schema = pathlib.Path("/tmp/schema.json")
    argv = _handler_with(skip_git_repo_check=True)._build_command(
        settings, answer, schema, continue_session=ref
    )
    assert argv[:9] == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "resume",
    ]
    json_index = argv.index("--json")
    assert json_index > argv.index("resume")
    assert argv[argv.index("--model") + 1] == "gpt-5"
    assert argv.index("--skip-git-repo-check") > argv.index("resume")
    assert argv[argv.index("--output-schema") + 1] == str(schema)
    assert argv[-2:] == [MAIN_THREAD_ID, "-"]


@pytest.mark.parametrize("mode", list(CodexSandboxMode))
def test_continuation_keeps_every_sandbox_mode(mode: CodexSandboxMode) -> None:
    """Every sandbox mode stays an explicit parent-command option on resume."""
    ref = AgentSessionRef(
        backend=AgentBackend.CODEX,
        session_id=MAIN_THREAD_ID,
        project_dir=ResourceUri("file:///ws/proj"),
    )
    settings = codex_agent_handler._CodexRunSettings(
        model="m", thinking=None, settle_timeout_sec=900.0
    )
    argv = _handler_with(sandbox_mode=mode)._build_command(
        settings, pathlib.Path("/tmp/a.txt"), None, continue_session=ref
    )
    assert argv[argv.index("--sandbox") + 1] == mode.value
    assert argv[argv.index("--ask-for-approval") + 1] == "never"


async def test_continuation_rejects_non_uuid_ids_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    """Names and fragments fail before any backend, project or lookup work."""
    project_dir = _project_dir(tmp_path)
    for bad_id in ("", "thread-1", MAIN_THREAD_ID[:8], MAIN_THREAD_ID.replace("-", "")):
        runner = _RecordingCommandRunner()
        result = await _run(
            FakeScenario.SUCCESS,
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
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=_session_ref(project_dir, backend=AgentBackend.CLAUDE_CODE),
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "claude_code" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_rejects_foreign_project_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    ref = AgentSessionRef(
        backend=AgentBackend.CODEX,
        session_id=MAIN_THREAD_ID,
        project_dir=ResourceUri("file:///elsewhere"),
    )
    result = await _run(
        FakeScenario.SUCCESS,
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
    """An unknown role is reported even when no native session exists for it."""
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        FakeScenario.SUCCESS,
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


async def test_continuation_missing_session_beats_invalid_timeout(
    tmp_path: pathlib.Path,
) -> None:
    """A deleted session is reported, not the conflicting timeout value."""
    project_dir = _project_dir(tmp_path)
    runner = _RecordingCommandRunner()
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=_session_ref(project_dir),
        handler_config={"settle_timeout_sec": 0},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "settle_timeout_sec" not in (result.error or "")
    assert "not found" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_invalid_timeout_fails_without_spawning(
    tmp_path: pathlib.Path,
) -> None:
    """With the session present, a bad timeout is still a pre-start failure."""
    project_dir = _project_dir(tmp_path)
    first = await _run(FakeScenario.SUCCESS, project_dir=project_dir)
    assert first.session is not None
    runner = _RecordingCommandRunner()
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"settle_timeout_sec": 0},
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "settle_timeout_sec" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_without_model_fails_before_spawn(
    tmp_path: pathlib.Path,
) -> None:
    """The saved session model is never silently reused on continuation."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m1"}
    )
    assert first.session is not None
    runner = _RecordingCommandRunner()
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        service_overrides={icommandrunner.ICommandRunner: runner},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "model" in (result.error or "")
    assert "new-run default" in (result.error or "")
    assert runner.start_long_running_calls == 0
    assert runner.run_calls == 0


async def test_continuation_schema_write_failure_has_no_recorded_prompt(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schema preparation precedes launch: a write failure starts nothing."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    record = tmp_path / "schema-fail-record.json"

    def _fail_write_text(self: pathlib.Path, *args: Any, **kwargs: Any) -> str | None:
        raise OSError("disk is gone")

    monkeypatch.setattr(pathlib.Path, "write_text", _fail_write_text)
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        output_schema={"type": "object"},
        record_path=record,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "schema" in (result.error or "")
    assert not record.exists()


async def test_continuation_missing_session_fails_and_creates_no_session(
    tmp_path: pathlib.Path,
) -> None:
    """The driver re-reads the native store; a miss creates nothing."""
    project_dir = _project_dir(tmp_path)
    home = pathlib.Path(os.environ["CODEX_HOME"])
    ref = _session_ref(project_dir, session_id="22222222-2222-4222-8222-222222222222")
    files_before = _native_files(home)
    record = tmp_path / "miss-record.json"
    result = await _run(
        FakeScenario.SUCCESS,
        prompt="hello?",
        project_dir=project_dir,
        continue_session=ref,
        record_path=record,
    )

    assert result.status is AgentRunStatus.FAILED
    assert "not found" in (result.error or "")
    assert "settle_timeout" not in (result.error or "")
    assert result.session is None
    assert _native_files(home) == files_before


async def test_continuation_deleted_session_fails_without_creating_one(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session removed after the handler lookup still fails at resume."""
    project_dir = _project_dir(tmp_path)
    home = pathlib.Path(os.environ["CODEX_HOME"])
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    files_before = _native_files(home)

    real_locate = codex_agent_handler.codex_history.locate_codex_history

    def _locate_then_delete(state: Any, root: pathlib.Path) -> Any:
        observation = real_locate(state, root)
        for path in (root / "sessions").rglob("rollout-*.jsonl"):
            if path.is_file():
                path.unlink()
        return observation

    monkeypatch.setattr(
        codex_agent_handler.codex_history, "locate_codex_history", _locate_then_delete
    )
    result = await _run(
        FakeScenario.SUCCESS,
        prompt="hello?",
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "no session found" in (result.error or "")
    assert result.session is None
    assert _native_files(home) <= files_before


async def test_continuation_settled_without_reported_id_fails_and_keeps_output(
    tmp_path: pathlib.Path,
) -> None:
    """A settled continuation with no stream identity fails without losing text."""
    project_dir = _project_dir(tmp_path)
    first = await _run(FakeScenario.SUCCESS, project_dir=project_dir)
    assert first.session is not None
    lines = [json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}})]
    process: Any = _ScriptedProcess(lines=lines, alive=[False])
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
        service_overrides={
            icommandrunner.ICommandRunner: _StubRunner(process, "kept text")
        },
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "kept text"
    assert "session" in (result.error or "").lower()
    assert result.session is None
    assert result.observation is None


async def test_continuation_missing_id_with_capture_reports_gap_not_session(
    tmp_path: pathlib.Path,
) -> None:
    """Capture on a missing identity reports the gap; the outcome still fails."""
    project_dir = _project_dir(tmp_path)
    first = await _run(FakeScenario.SUCCESS, project_dir=project_dir)
    assert first.session is not None
    lines = [json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}})]
    process: Any = _ScriptedProcess(lines=lines, alive=[False])
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        capture_history=True,
        handler_config={"model": "m"},
        service_overrides={
            icommandrunner.ICommandRunner: _NoProbeRunner(process, "kept text")
        },
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "kept text"
    assert result.session is None
    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.UNAVAILABLE
    assert result.observation.gaps


async def test_continuation_failure_without_reported_id_keeps_its_reason(
    tmp_path: pathlib.Path,
) -> None:
    """A refused backend outcome is not relabeled as a missing identity."""
    project_dir = _project_dir(tmp_path)
    first = await _run(FakeScenario.SUCCESS, project_dir=project_dir)
    assert first.session is not None
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "9", "type": "agent_message", "text": "nope"},
            }
        ),
        json.dumps({"type": "turn.failed", "error": {"message": "denied"}}),
    ]
    process: Any = _ScriptedProcess(lines=lines, alive=[False])
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
        service_overrides={icommandrunner.ICommandRunner: _StubRunner(process, "nope")},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "denied"
    assert result.session is None


async def test_continuation_structured_output_round_trip(
    tmp_path: pathlib.Path,
) -> None:
    """A resumed turn extracts JSON against the schema like a new turn does."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    record = tmp_path / "structured-resume-record.json"
    result = await _run(
        FakeScenario.STRUCTURED_VALID,
        answer={"answer": "42"},
        output_schema=schema,
        project_dir=project_dir,
        continue_session=first.session,
        record_path=record,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.structured_output == {"answer": "42"}
    assert result.session == first.session
    driver = _read_record(record)
    assert driver["schema"] == schema


async def test_continuation_malformed_structured_output_fails_and_keeps_text(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    result = await _run(
        FakeScenario.STRUCTURED_INVALID,
        answer="not json{{{",
        output_schema={"type": "object"},
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output == "not json{{{"
    assert result.structured_output is None
    assert result.session is None


async def test_continuation_backend_failure_keeps_its_reason(
    tmp_path: pathlib.Path,
) -> None:
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    result = await _run(
        FakeScenario.FAILED,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "boom"
    assert result.session is None


async def test_continuation_timeout_fails_and_tears_down(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run that hangs is bounded by the current timeout setting."""
    monkeypatch.setattr(codex_agent_handler, "_EXIT_GRACE_SEC", 0.3)
    monkeypatch.setattr(codex_agent_handler, "_SIGNAL_GRACE_SEC", 0.3)
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    record = tmp_path / "hang-resume-record.json"
    result = await _run(
        FakeScenario.HANG,
        project_dir=project_dir,
        continue_session=first.session,
        record_path=record,
        handler_config={"model": "m", "settle_timeout_sec": 0.5},
    )

    assert result.status is AgentRunStatus.FAILED
    assert "did not finish within" in (result.error or "")
    assert result.session is None
    driver = _read_record(record)
    assert await _wait_gone(driver["agent_pid"])


class _NoProbeRunner(_StubRunner):
    """A scripted process runner whose version probe looks like no binary."""

    async def run(
        self,
        cmd: icommandrunner.Argv,
        cwd: pathlib.Path | None = None,
        env: dict[str, str] | None = None,
        new_process_group: bool = False,
    ) -> Any:
        _ = (cmd, cwd, env, new_process_group)
        raise FileNotFoundError(2, "No such file or directory")


class _BrokenPipeProcess(_ScriptedProcess):
    def __init__(self, error: Exception) -> None:
        super().__init__(lines=[], alive=[False])
        self._error = error

    def write_to_stdin(self, value: str) -> None:
        _ = value
        raise self._error


@pytest.mark.parametrize(
    "error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")]
)
async def test_continuation_prompt_pipe_failure_is_an_explicit_failure(
    tmp_path: pathlib.Path, error: Exception
) -> None:
    """A native process that exits before reading the prompt fails the turn."""
    project_dir = _project_dir(tmp_path)
    first = await _run(FakeScenario.SUCCESS, project_dir=project_dir)
    assert first.session is not None
    result = await _run(
        FakeScenario.SUCCESS,
        project_dir=project_dir,
        continue_session=first.session,
        handler_config={"model": "m"},
        service_overrides={
            icommandrunner.ICommandRunner: _StubRunner(
                _BrokenPipeProcess(error), "unused"
            )
        },
    )

    assert result.status is AgentRunStatus.FAILED
    assert "pipe" in (result.error or "")
    assert result.session is None


async def test_continuation_capture_locates_the_same_main_session(
    tmp_path: pathlib.Path,
) -> None:
    """Capture on a continuation observes the resumed native session."""
    project_dir = _project_dir(tmp_path)
    home = pathlib.Path(os.environ["CODEX_HOME"])
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    result = await _run(
        FakeScenario.SUCCESS,
        prompt="more",
        project_dir=project_dir,
        continue_session=first.session,
        capture_history=True,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.session == first.session
    observation = result.observation
    assert observation is not None
    assert observation.history_status is HistoryDiscoveryStatus.AVAILABLE
    assert observation.session_id == MAIN_THREAD_ID
    main = next(f for f in observation.files if f.role is NativeHistoryRole.MAIN)
    assert resource_uri_to_path(main.source_uri) == _main_rollout(home, MAIN_THREAD_ID)


async def test_continuation_observation_failure_keeps_reference_and_status(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken history read degrades the observation, not the settled turn."""
    project_dir = _project_dir(tmp_path)
    first = await _run(
        FakeScenario.SUCCESS, project_dir=project_dir, handler_config={"model": "m"}
    )
    assert first.session is not None
    calls: list[int] = []
    real_locate = codex_agent_handler.codex_history.locate_codex_history

    def _flaky_locate(state: Any, root: pathlib.Path) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return real_locate(state, root)
        raise RuntimeError("disk is gone")

    monkeypatch.setattr(
        codex_agent_handler.codex_history, "locate_codex_history", _flaky_locate
    )
    result = await _run(
        FakeScenario.SUCCESS,
        prompt="more",
        project_dir=project_dir,
        continue_session=first.session,
        capture_history=True,
        handler_config={"model": "m"},
    )

    assert result.status is AgentRunStatus.SETTLED
    assert result.output == "done"
    assert result.session == first.session
    assert result.observation is not None
    assert result.observation.history_status is HistoryDiscoveryStatus.FAILED
