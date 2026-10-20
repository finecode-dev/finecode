"""Tests for the bridge that resolves lint-fix code actions to their edits."""

from __future__ import annotations

import pathlib
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_lint.apply_code_actions_action import TextEditOperation
from fine_lint.get_lint_fixes_action import (
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from fine_lint.lint_fix import LintFix, Position, Range, TextEdit
from fine_lint.lint_fixes_code_actions_bridge_handler import PROVIDER_ID
from fine_lint.lint_fixes_resolve_bridge_handler import LintFixesResolveBridgeHandler
from fine_lint.resolve_code_action_action import ResolveCodeActionRunPayload

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)
_FILE_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.py"))
_ZERO_RANGE = Range(
    start=Position(line=0, character=0), end=Position(line=0, character=0)
)


class _RunContextStub:
    """Only the two attributes the bridge handler reads off the run context."""

    def __init__(self, meta: code_action.RunActionMeta, file_version: str) -> None:
        self.meta = meta
        self.file_version = file_version


class _StubActionRunner:
    """Answers get_lint_fixes with a fixed result and records whether it was called."""

    def __init__(self, result: GetLintFixesRunResult) -> None:
        self._result = result
        self.call_count = 0
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
        self.call_count += 1
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


def _make_fix(fix_id: str) -> LintFix:
    return LintFix(
        fix_id=fix_id,
        title=f"Fix {fix_id}",
        kind="quickfix",
        edits={_FILE_URI: [TextEdit(range=_ZERO_RANGE, new_text="fixed")]},
        target_range=_ZERO_RANGE,
        target_codes=["F401"],
    )


async def test_a_foreign_provider_is_left_untouched() -> None:
    """A selection minted by some other provider must not trigger this bridge's
    work at all -- if it did, a resolve request could recompute an entire file's
    fixes on every handler regardless of which one actually owns the action, and
    two handlers could both answer for the same id.
    """
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="v1", fixes=[])
    )
    handler = LintFixesResolveBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="v1")

    result = await handler.run(
        ResolveCodeActionRunPayload(
            provider="some_other_provider",
            action_id="whatever",
            file_path=_FILE_URI,
        ),
        typing.cast(typing.Any, run_context),
    )

    assert result.operations is None
    assert action_runner.call_count == 0


async def test_a_known_action_id_resolves_to_the_same_operations_twice() -> None:
    """Resolve must be safe to call more than once for the same action -- an IDE
    may resend ``codeAction/resolve``, or a future apply workflow may resolve a
    stub it received earlier -- and unchanged content must not change the answer.
    """
    fix = _make_fix("ruff:F401:0:0:0")
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="v1", fixes=[fix])
    )
    handler = LintFixesResolveBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="v1")
    payload = ResolveCodeActionRunPayload(
        provider=PROVIDER_ID,
        action_id="ruff:F401:0:0:0",
        file_path=_FILE_URI,
    )

    first = await handler.run(payload, typing.cast(typing.Any, run_context))
    second = await handler.run(payload, typing.cast(typing.Any, run_context))

    assert first.operations == second.operations
    assert first.operations == [
        TextEditOperation(
            file_path=_FILE_URI,
            edits=fix.edits[_FILE_URI],
            file_version="v1",
        )
    ]


async def test_an_action_id_that_no_longer_exists_yields_no_operations() -> None:
    """A stale action_id -- naming a fix that was already applied, or that
    disappeared because the file changed -- must be reported as unresolved rather
    than raise, or a single stale resolve request would fail the whole
    ``codeAction/resolve`` call for an editor.
    """
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="v1", fixes=[])
    )
    handler = LintFixesResolveBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="v1")

    result = await handler.run(
        ResolveCodeActionRunPayload(
            provider=PROVIDER_ID,
            action_id="no-such-id",
            file_path=_FILE_URI,
        ),
        typing.cast(typing.Any, run_context),
    )

    assert result.operations is None


async def test_a_fix_editing_a_different_file_than_requested_carries_no_version_for_it() -> (
    None
):
    """A fix that edits a file other than the one resolve was asked about
    (`applying-code-actions` Q1, e.g. a structural fix that belongs in a sibling module)
    must not stamp that other file with the requested file's pinned version --
    only the requested file's version was actually pinned by this run, and
    reusing it for an unrelated file is exactly the bug ADR-0083 rule 5 fixes.
    """
    other_uri = path_to_resource_uri(pathlib.Path("/tmp/other.py"))
    fix = LintFix(
        fix_id="ruff:F401:0:0:0",
        title="Fix",
        kind="quickfix",
        edits={
            _FILE_URI: [TextEdit(range=_ZERO_RANGE, new_text="fixed")],
            other_uri: [TextEdit(range=_ZERO_RANGE, new_text="also fixed")],
        },
        target_range=_ZERO_RANGE,
        target_codes=["F401"],
    )
    action_runner = _StubActionRunner(
        GetLintFixesRunResult(file_version="v1", fixes=[fix])
    )
    handler = LintFixesResolveBridgeHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        )
    )
    run_context = _RunContextStub(meta=_META, file_version="v1")

    result = await handler.run(
        ResolveCodeActionRunPayload(
            provider=PROVIDER_ID,
            action_id="ruff:F401:0:0:0",
            file_path=_FILE_URI,
        ),
        typing.cast(typing.Any, run_context),
    )

    assert result.operations is not None
    by_file = {op.file_path: op for op in result.operations}
    assert by_file[_FILE_URI].file_version == "v1"
    assert by_file[other_uri].file_version is None
