"""Routing `run_agent_task` to the Codex backend with a real fake-codex subprocess."""

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

from fine_agent_codex.codex_agent_handler import CodexAgentHandler
from fine_agent_codex.run_codex_agent_task_action import RunCodexAgentTaskAction

_FAKE_CODEX = pathlib.Path(__file__).parent / "fake_codex.py"
_LEAF_SOURCE = (
    f"{RunCodexAgentTaskAction.__module__}.{RunCodexAgentTaskAction.__qualname__}"
)
_RECORD_NAME = "driver-codex.json"


class _FakeCodexHandler(CodexAgentHandler):
    def _executable(self) -> list[str]:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        return [
            sys.executable,
            str(_FAKE_CODEX),
            "success",
            str(project_dir / _RECORD_NAME),
            json.dumps("answer-codex"),
        ]


class CodexDriverRecord(typing.TypedDict):
    argv: list[str]
    prompt: str


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
    """Argv and prompt of the run that rewrote the driver record."""
    after = path.read_bytes()
    assert after != before
    record: CodexDriverRecord = json.loads(after.decode("utf-8"))
    return list(record["argv"]), str(record["prompt"])


def _native_session_files(tmp_path: pathlib.Path) -> set[str]:
    base = tmp_path / "codex-home"
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
            project_name="codex-project",
            actions={
                "codex_leaf": {
                    "source": _LEAF_SOURCE,
                    "handlers": [
                        {
                            "name": "codex_leaf",
                            "source": (
                                f"{_FakeCodexHandler.__module__}"
                                f".{_FakeCodexHandler.__qualname__}"
                            ),
                            "config": config,
                        }
                    ],
                }
            },
        )
    )
    return HarnessTarget(session=session, action_name="codex_leaf")


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

    assert _BACKEND_SOURCES[AgentBackend.CODEX] == _LEAF_SOURCE


async def test_router_selects_codex_with_real_process(tmp_path: pathlib.Path) -> None:
    """Role and default routes reach the Codex leaf, verified by process records."""
    assert _FAKE_CODEX.exists()

    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(
            stack,
            tmp_path,
            {
                "model": "codex-default",
                "profiles": {"bundle": {"model": "codex-bundle"}},
            },
        )
        adapter = _HarnessActionRunner({_LEAF_SOURCE: target})
        router_session = await _router_session(
            stack,
            tmp_path,
            adapter,
            {
                "default_backend": "codex",
                "profiles": {"codex_role": {"backend": "codex", "profile": "bundle"}},
            },
        )

        record = tmp_path / _RECORD_NAME
        cases: list[tuple[str | None, str | None, str]] = [
            ("codex_role", "bundle", "codex-bundle"),
            (None, None, "codex-default"),
        ]
        for role, bundle, model in cases:
            prompt = f"prompt-{role}-{model}"
            before = _read(record)
            calls_before = len(adapter.calls)
            payload = RunAgentTaskRunPayload(prompt=prompt, profile=role)
            result = await router_session.run_action("run_agent_task", payload)
            assert isinstance(result, RunAgentTaskRunResult)
            assert result.status is AgentRunStatus.SETTLED
            assert result.output == "answer-codex"
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
            RunAgentTaskRunPayload(prompt="prompt-missing", profile="codex_role"),
        )
        assert missing_result.status is AgentRunStatus.FAILED
        assert _LEAF_SOURCE in (missing_result.error or "")
        assert len(adapter.calls) == missing_calls + 1
        assert _read(record) == missing_before


async def test_direct_continuation_reuses_the_same_native_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second prompt resumes the reported session; no extra session appears."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(stack, tmp_path, {"model": "codex-default"})
        first = await target.session.run_action(
            target.action_name, RunAgentTaskRunPayload(prompt="first")
        )
        assert isinstance(first, RunAgentTaskRunResult)
        assert first.status is AgentRunStatus.SETTLED
        assert first.output == "answer-codex"
        assert first.session is not None
        assert first.session.backend is AgentBackend.CODEX
        files_before = _native_session_files(tmp_path)
        assert files_before, "no native session file"
        second = await target.session.run_action(
            target.action_name,
            RunAgentTaskRunPayload(prompt="second", continue_session=first.session),
        )
        assert isinstance(second, RunAgentTaskRunResult)
        assert second.status is AgentRunStatus.SETTLED
        assert second.output == "answer-codex"
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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    async with contextlib.AsyncExitStack() as stack:
        target = await _leaf_target(stack, tmp_path, {"model": "codex-default"})
        adapter = _HarnessActionRunner({_LEAF_SOURCE: target})
        router_session = await _router_session(
            stack, tmp_path, adapter, {"default_backend": "codex"}
        )
        first = await router_session.run_action(
            "run_agent_task", RunAgentTaskRunPayload(prompt="first")
        )
        assert isinstance(first, RunAgentTaskRunResult)
        assert first.status is AgentRunStatus.SETTLED
        assert first.session is not None
        assert first.session.backend is AgentBackend.CODEX
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
