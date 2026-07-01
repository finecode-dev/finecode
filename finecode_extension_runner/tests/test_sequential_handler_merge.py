from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from finecode_extension_api import code_action
from finecode_extension_runner.testing import handler_test_session

pytestmark = pytest.mark.anyio


@dataclasses.dataclass
class _MergeRunResult(code_action.RunActionResult):
    """Accumulates via += for every key, like GroupSrcArtifactFilesByLangRunResult
    or DiagnosticFilesRunResult — the pattern that exposed the self-merge bug."""

    values: dict[str, list[str]]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _MergeRunResult):
            return
        for key, values in other.values.items():
            if key not in self.values:
                self.values[key] = values
            else:
                self.values[key] += values


class _MergeTestAction(
    code_action.Action[
        code_action.RunActionPayload, code_action.RunActionContext, _MergeRunResult
    ]
):
    # HANDLER_EXECUTION defaults to SEQUENTIAL — the branch where the bug lived.
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = _MergeRunResult


class _FirstHandler(
    code_action.ActionHandler[_MergeTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> _MergeRunResult:
        return _MergeRunResult(values={"a": ["1"]})


class _SecondHandler(
    code_action.ActionHandler[_MergeTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> _MergeRunResult:
        return _MergeRunResult(values={"b": ["2"]})


_ACTION_NAME = _MergeTestAction.__name__
_ACTION_SOURCE = f"{_MergeTestAction.__module__}.{_MergeTestAction.__qualname__}"
_FIRST_SOURCE = f"{_FirstHandler.__module__}.{_FirstHandler.__qualname__}"
_SECOND_SOURCE = f"{_SecondHandler.__module__}.{_SecondHandler.__qualname__}"

_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [
            {"name": "first", "source": _FIRST_SOURCE},
            {"name": "second", "source": _SECOND_SOURCE},
        ],
    }
}


async def test_sequential_handlers_do_not_double_accumulated_values(
    tmp_path: Path,
) -> None:
    """Regression test for a self-merge bug in run_action's sequential branch.

    With 2+ SEQUENTIAL handlers, the merge loop used to call
    run_context_info.update(action_result) after every handler. Once the first
    handler ran, action_result and run_context_info's tracked current_result
    became the same object (aliased, not copied). Calling update() on it again
    after later handlers merged the object into itself, doubling every
    already-accumulated value (e.g. a classified file listed twice, which
    downstream code silently dropped as "No coroutines scheduled").
    """
    async with handler_test_session(
        project_dir=tmp_path, actions=_ACTIONS
    ) as session:
        result = await session.run_action(_ACTION_NAME)

    assert isinstance(result, _MergeRunResult)
    assert result.values == {"a": ["1"], "b": ["2"]}
