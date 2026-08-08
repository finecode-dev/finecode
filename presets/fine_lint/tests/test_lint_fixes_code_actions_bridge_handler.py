"""Tests for the bridge that turns lint fixes into code actions."""

from __future__ import annotations

import pathlib
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_lint.get_code_actions_action import (
    GetCodeActionsRunPayload,
)
from fine_lint.get_lint_fixes_action import (
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from fine_lint.lint_fix import LintFix, Position, Range
from fine_lint.lint_fixes_code_actions_bridge_handler import (
    PROVIDER_ID,
    LintFixesCodeActionsBridgeHandler,
)

_WHOLE_FILE_RANGE = Range(
    start=Position(line=0, character=0), end=Position(line=0, character=0)
)
_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)
_FILE_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.py"))


class _RunContextStub:
    """Only the two attributes the bridge handler reads off the run context."""

    def __init__(self, meta: code_action.RunActionMeta, file_version: str) -> None:
        self.meta = meta
        self.file_version = file_version


class _StubActionRunner:
    """Answers get_lint_fixes with a fixed result and records the payload it saw."""

    def __init__(self, result: GetLintFixesRunResult) -> None:
        self._result = result
        self.seen_payload: GetLintFixesRunPayload | None = None

    async def get_actions_for_parent(
        self, parent_action_type: type
    ) -> dict[str, iprojectactionrunner.ActionRef]:
        raise NotImplementedError

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        self.seen_payload = payload
        return self._result

    def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        raise NotImplementedError


def _make_fix(fix_id: str, line: int) -> LintFix:
    return LintFix(
        fix_id=fix_id,
        title=f"Fix {fix_id}",
        kind="quickfix",
        edits={},
        target_range=Range(
            start=Position(line=line, character=0), end=Position(line=line, character=0)
        ),
        target_codes=[],
    )


async def test_bridge_stamps_provider_on_every_action_it_returns() -> None:
    """A caller must be able to tell which provider offered an action, or nothing can
    ever route a later resolve/apply request back to the code that can honour it.
    """
    fixes = [_make_fix("ruff:F401:0:0:0", 0), _make_fix("ruff:F401:1:0:0", 1)]
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="v1", fixes=fixes)
    )
    handler = LintFixesCodeActionsBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="v1")

    result = await handler.run(
        GetCodeActionsRunPayload(
            file_path=_FILE_URI,
            range=_WHOLE_FILE_RANGE,
            diagnostics=[],
        ),
        typing.cast(typing.Any, run_context),
    )

    assert len(result.actions) == 2
    assert all(action.provider == PROVIDER_ID for action in result.actions)


async def test_bridge_forwards_the_runs_pinned_version_not_the_callers_guard() -> None:
    """The sub-action must compute fixes against the version this run pinned, not
    whatever staleness guard the caller happened to send.

    Before this forwarding existed, the LSP endpoint never sent a version at all, so
    the sub-action's guard was always ``None`` and nothing was pinned -- a
    concurrently-changing file could produce fixes computed against two different
    contents in the same response.
    """
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="pinned-version", fixes=[])
    )
    handler = LintFixesCodeActionsBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="pinned-version")

    await handler.run(
        GetCodeActionsRunPayload(
            file_path=_FILE_URI,
            range=_WHOLE_FILE_RANGE,
            diagnostics=[],
            # The caller's own guard deliberately disagrees with the run's pinned
            # version, so the assertion below can tell which one actually reached
            # the sub-action.
            file_version="caller-guard-version",
        ),
        typing.cast(typing.Any, run_context),
    )

    assert action_runner.seen_payload is not None
    assert action_runner.seen_payload.file_version == "pinned-version"
