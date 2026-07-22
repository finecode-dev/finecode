from __future__ import annotations

import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _NoopResult(code_action.RunActionResult):
    def update(self, other: code_action.RunActionResult) -> None: ...

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class _ProgressContext(code_action.RunActionContext[code_action.RunActionPayload]): ...


class _ProgressAction(
    code_action.Action[code_action.RunActionPayload, _ProgressContext, _NoopResult]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = _ProgressContext
    RESULT_TYPE = _NoopResult


class _ProgressReportingHandler(
    code_action.ActionHandler[_ProgressAction, code_action.ActionHandlerConfig]
):
    """Reports progress the way a real handler would, via run_context.progress()."""

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _ProgressContext,
    ) -> _NoopResult:
        async with run_context.progress("working", total=2) as progress:
            await progress.advance(1, message="step 1")
            await progress.advance(1, message="step 2")
        return _NoopResult()


_ACTION_NAME = _ProgressAction.__name__
_ACTIONS = {
    _ACTION_NAME: {
        "source": f"{_ProgressAction.__module__}.{_ProgressAction.__qualname__}",
        "handlers": [
            {
                "name": "progress",
                "source": (
                    f"{_ProgressReportingHandler.__module__}."
                    f"{_ProgressReportingHandler.__qualname__}"
                ),
            }
        ],
    }
}


async def test_session_progress_collects_handler_progress_events(tmp_path: Path) -> None:
    """`session.progress` must observe the begin/report/end sequence a handler
    emits via run_context.progress() -- otherwise assertions against it pass
    vacuously against an empty list."""
    async with handler_test_session(project_dir=tmp_path, actions=_ACTIONS) as session:
        result = await session.run_action(_ACTION_NAME)

    assert result is not None
    event_types = [event["type"] for event in session.progress.events]
    assert event_types == ["begin", "report", "report", "end"]
