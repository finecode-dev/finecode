"""Routing `run_agent_task` to the Claude Code backend with a real fake-claude subprocess."""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import json
import pathlib
import sys
import typing

import pytest
from fine_agent.history_types import AgentBackend
from fine_agent.route_agent_task_handler import RouteAgentTaskHandler
from fine_agent.run_agent_task_action import (
    AgentRunStatus,
    RunAgentTaskAction,
    RunAgentTaskRunPayload,
    RunAgentTaskRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner._converter import converter
from finecode_extension_runner.testing import Session, handler_test_session

from fine_agent_claude_code.claude_code_agent_handler import ClaudeCodeAgentHandler
from fine_agent_claude_code.run_claude_code_agent_task_action import (
    RunClaudeCodeAgentTaskAction,
)

_FAKE_CLAUDE = pathlib.Path(__file__).parent / "fake_claude.py"
_LEAF_SOURCE = (
    f"{RunClaudeCodeAgentTaskAction.__module__}"
    f".{RunClaudeCodeAgentTaskAction.__qualname__}"
)
_RECORD_NAME = "driver-claude_code.jsonl"


_CLAUDE_SESSION_ID = "22222222-2222-4222-8222-222222222222"


class _FakeClaudeHandler(ClaudeCodeAgentHandler):
    def _executable(self) -> list[str]:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        scenario = {
            "record_path": str(project_dir / _RECORD_NAME),
            "record_driver": True,
            "session_id": _CLAUDE_SESSION_ID,
            "config_dir": str(project_dir / "claude-home"),
            "write_native": True,
            "steps": [
                {"do": "read_prompt"},
                {"do": "init", "session_id": _CLAUDE_SESSION_ID},
                {
                    "do": "emit",
                    "frame": {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "answer-claude",
                        "session_id": _CLAUDE_SESSION_ID,
                        "num_turns": 1,
                        "permission_denials": [],
                    },
                },
            ],
        }
        return [sys.executable, str(_FAKE_CLAUDE), json.dumps(scenario)]


class DriverArgv(typing.TypedDict):
    argv: list[str]


class DriverPrompt(typing.TypedDict):
    prompt: str


NativeDriverLine = DriverArgv | DriverPrompt


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    session: Session
    action_name: str


@dataclasses.dataclass(frozen=True)
class HarnessDispatchCall:
    ref: iprojectactionrunner.ActionRef
    payload: RunAgentTaskRunPayload
    meta: code_action.RunActionMeta


class _HarnessActionRunner:
    def __init__(self, targets: dict[str, HarnessTarget]) -> None:
        self.targets = targets
        self.calls: list[HarnessDispatchCall] = []

    async def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: RunAgentTaskRunPayload,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
        **prototype_options: object,
    ) -> collections.abc.AsyncIterator[RunAgentTaskRunResult]:
        assert action_type.action_type is None
        assert action_type.result_type is RunAgentTaskRunResult
        assert caller_kwargs is None
        self.calls.append(
            HarnessDispatchCall(
                ref=action_type,
                payload=dataclasses.replace(payload),
                meta=meta,
            )
        )
        target = self.targets.get(action_type.source)
        if target is None:
            raise iprojectactionrunner.ActionNotFound(
                f"no harness target for {action_type.source!r}"
            )
        result = await target.session.run_action(target.action_name, payload)
        assert isinstance(result, RunAgentTaskRunResult)
        yield result


def _read(path: pathlib.Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _driver_evidence(before: bytes | None, path: pathlib.Path) -> tuple[list[str], str]:
    """Argv and prompt of the run that appended to the driver record."""
    text_before = before.decode("utf-8") if before is not None else ""
    text_after = path.read_bytes().decode("utf-8")
    assert text_after.startswith(text_before)
    assert len(text_after) > len(text_before)
    argv: list[str] | None = None
    prompt: str | None = None
    for line in text_after[len(text_before) :].splitlines():
        if not line.strip():
            continue
        entry: NativeDriverLine = json.loads(line)
        if "argv" in entry:
            argv = list(typing.cast(DriverArgv, entry)["argv"])
        if "prompt" in entry:
            prompt = str(typing.cast(DriverPrompt, entry)["prompt"])
    assert argv is not None, f"no argv row appended to {path.name}"
    assert prompt is not None, f"no prompt row appended to {path.name}"
    return argv, prompt


def _native_session_files(tmp_path: pathlib.Path) -> set[str]:
    base = tmp_path / "claude-home"
    if not base.is_dir():
        return set()
    return {str(p.relative_to(tmp_path)) for p in base.rglob("*") if p.is_file()}


async def _leaf_target(
    stack: contextlib.AsyncExitStack,
    tmp_path: pathlib.Path,
    config: dict[str, object],
) -> HarnessTarget:
    session = await stack.enter_async_context(
        handler_test_session(
            project_dir=tmp_path,
            project_name="claude-project",
            actions={
                "claude_leaf": {
                    "source": _LEAF_SOURCE,
                    "handlers": [
                        {
                            "name": "claude_leaf",
                            "source": (
                                f"{_FakeClaudeHandler.__module__}"
                                f".{_FakeClaudeHandler.__qualname__}"
                            ),
                            "config": config,
                        }
                    ],
                }
            },
        )
    )
    return HarnessTarget(session=session, action_name="claude_leaf")


async def _router_session(
    stack: contextlib.AsyncExitStack,
    tmp_path: pathlib.Path,
    adapter: _HarnessActionRunner,
    config: dict[str, object],
) -> Session:
    return await stack.enter_async_context(
        handler_test_session(
            project_dir=tmp_path,
            project_name="router-project",
            actions={
                "run_agent_task": {
                    "source": (
                        f"{RunAgentTaskAction.__module__}"
                        f".{RunAgentTaskAction.__qualname__}"
                    ),
                    "handlers": [
                        {
                            "name": "router",
                            "source": (
                                f"{RouteAgentTaskHandler.__module__}"
                                f".{RouteAgentTaskHandler.__qualname__}"
                            ),
                            "config": config,
                        }
                    ],
                }
            },
            service_overrides={iprojectactionrunner.IProjectActionRunner: adapter},
        )
    )


def test_canonical_source_matches_action_class() -> None:
    """The router's table string tracks the real action class location."""
    from fine_agent.route_agent_task_handler import _BACKEND_SOURCES

    assert _BACKEND_SOURCES[AgentBackend.CLAUDE_CODE] == _LEAF_SOURCE


async def test_router_selects_claude_code_with_real_process(tmp_path: pathlib.Path) -> None:
    """Role and default routes reach the Claude Code leaf, verified by process records."""
    assert _FAKE_CLAUDE.exists()

    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(
            stack,
            tmp_path,
            {
                "model": "claude-default",
                "profiles": {"bundle": {"model": "claude-bundle"}},
            },
        )
        adapter = _HarnessActionRunner({_LEAF_SOURCE: target})
        router_session = await _router_session(
            stack,
            tmp_path,
            adapter,
            {
                "default_backend": "claude_code",
                "profiles": {
                    "claude_role": {"backend": "claude_code", "profile": "bundle"}
                },
            },
        )

        record = tmp_path / _RECORD_NAME
        cases: list[tuple[str | None, str | None, str]] = [
            ("claude_role", "bundle", "claude-bundle"),
            (None, None, "claude-default"),
        ]
        for role, bundle, model in cases:
            prompt = f"prompt-{role}-{model}"
            before = _read(record)
            calls_before = len(adapter.calls)
            payload = RunAgentTaskRunPayload(prompt=prompt, profile=role)
            result = await router_session.run_action("run_agent_task", payload)
            assert isinstance(result, RunAgentTaskRunResult)
            assert result.status is AgentRunStatus.SETTLED
            assert result.output == "answer-claude"
            assert result.error is None
            assert payload.profile == role
            argv, recorded_prompt = _driver_evidence(before, record)
            assert recorded_prompt == prompt
            assert "--model" in argv
            assert argv[argv.index("--model") + 1] == model
            assert len(adapter.calls) == calls_before + 1
            call = adapter.calls[-1]
            assert call.ref.source == _LEAF_SOURCE
            assert call.ref.action_type is None
            assert call.ref.result_type is RunAgentTaskRunResult
            assert call.payload.profile == bundle
            assert call.payload.prompt == prompt

        unknown_before = _read(record)
        unknown_calls = len(adapter.calls)
        unknown_result = await router_session.run_action(
            "run_agent_task",
            RunAgentTaskRunPayload(
                prompt="prompt-unknown", profile="nope", capture_history=True
            ),
        )
        assert unknown_result.status is AgentRunStatus.FAILED
        assert unknown_result.duration_sec == 0.0
        assert len(adapter.calls) == unknown_calls
        assert _read(record) == unknown_before

        del adapter.targets[_LEAF_SOURCE]
        missing_before = _read(record)
        missing_calls = len(adapter.calls)
        missing_result = await router_session.run_action(
            "run_agent_task",
            RunAgentTaskRunPayload(prompt="prompt-missing", profile="claude_role"),
        )
        assert missing_result.status is AgentRunStatus.FAILED
        assert _LEAF_SOURCE in (missing_result.error or "")
        assert len(adapter.calls) == missing_calls + 1
        assert _read(record) == missing_before


async def test_direct_continuation_reuses_the_same_native_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second prompt resumes the reported session; no extra session appears."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(stack, tmp_path, {"model": "claude-default", "permission_mode": "acceptEdits"})
        first = await target.session.run_action(
            target.action_name, RunAgentTaskRunPayload(prompt="first")
        )
        assert isinstance(first, RunAgentTaskRunResult)
        assert first.status is AgentRunStatus.SETTLED
        assert first.output == "answer-claude"
        assert first.session is not None
        assert first.session.backend is AgentBackend.CLAUDE_CODE
        files_before = _native_session_files(tmp_path)
        assert files_before, "no native session file"
        second = await target.session.run_action(
            target.action_name,
            RunAgentTaskRunPayload(prompt="second", continue_session=first.session),
        )
        assert isinstance(second, RunAgentTaskRunResult)
        assert second.status is AgentRunStatus.SETTLED
        assert second.output == "answer-claude"
        assert second.session == first.session
        assert _native_session_files(tmp_path) == files_before
        third = await target.session.run_action(
            target.action_name,
            RunAgentTaskRunPayload(prompt="third", continue_session=second.session),
        )
        assert isinstance(third, RunAgentTaskRunResult)
        assert third.status is AgentRunStatus.SETTLED
        assert third.session == first.session
        assert _native_session_files(tmp_path) == files_before


async def test_router_continuation_forwards_the_reference_unchanged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing preserves the session reference; the leaf session is reused."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(stack, tmp_path, {"model": "claude-default", "permission_mode": "acceptEdits"})
        adapter = _HarnessActionRunner({_LEAF_SOURCE: target})
        router_session = await _router_session(
            stack, tmp_path, adapter, {"default_backend": "claude_code"}
        )
        first = await router_session.run_action(
            "run_agent_task", RunAgentTaskRunPayload(prompt="first")
        )
        assert isinstance(first, RunAgentTaskRunResult)
        assert first.status is AgentRunStatus.SETTLED
        assert first.session is not None
        assert first.session.backend is AgentBackend.CLAUDE_CODE
        assert len(adapter.calls) == 1
        assert adapter.calls[-1].payload.continue_session is None
        revived = converter.structure(
            converter.unstructure(adapter.calls[-1].payload),
            RunAgentTaskRunPayload,
        )
        assert revived.continue_session is None
        files_before = _native_session_files(tmp_path)
        assert files_before
        second = await router_session.run_action(
            "run_agent_task",
            RunAgentTaskRunPayload(prompt="second", continue_session=first.session),
        )
        assert isinstance(second, RunAgentTaskRunResult)
        assert second.status is AgentRunStatus.SETTLED
        assert second.session == first.session
        assert _native_session_files(tmp_path) == files_before
        assert len(adapter.calls) == 2
        forwarded = adapter.calls[-1].payload
        assert forwarded.continue_session == first.session
        assert forwarded.prompt == "second"
        revived_forwarded = converter.structure(
            converter.unstructure(forwarded), RunAgentTaskRunPayload
        )
        assert revived_forwarded.continue_session == first.session
        revived_result = converter.structure(
            converter.unstructure(second), RunAgentTaskRunResult
        )
        assert revived_result.session == first.session
