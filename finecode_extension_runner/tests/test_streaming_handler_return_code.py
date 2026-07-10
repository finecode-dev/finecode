from __future__ import annotations

import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _DiagResult(code_action.RunActionResult):
    """Shaped like DiagnosticFilesRunResult (fine_inspect_code): return_code
    reflects whether any diagnostic was reported."""

    messages: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _DiagResult):
            return
        self.messages.extend(other.messages)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return (
            code_action.RunReturnCode.ERROR
            if self.messages
            else code_action.RunReturnCode.SUCCESS
        )


class _DiagContext(code_action.RunActionContext[code_action.RunActionPayload]): ...


class _DiagAction(
    code_action.Action[code_action.RunActionPayload, _DiagContext, _DiagResult]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = _DiagContext
    RESULT_TYPE = _DiagResult
    # matches inspect_code/lint: concurrent handler execution
    HANDLER_EXECUTION = code_action.HandlerExecution.CONCURRENT


class _StreamingBridgeHandler(
    code_action.ActionHandler[_DiagAction, code_action.ActionHandlerConfig]
):
    """Mirrors LintInspectCodeBridgeHandler (fine_lint): reports its result only
    via partial_result_sender.send() and returns None from run(), instead of
    returning the aggregated result."""

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _DiagContext,
    ) -> None:
        await run_context.partial_result_sender.send(
            _DiagResult(messages=["file.py:1: error found"])
        )


_ACTION_NAME = _DiagAction.__name__
_ACTION_SOURCE = f"{_DiagAction.__module__}.{_DiagAction.__qualname__}"
_HANDLER_SOURCE = (
    f"{_StreamingBridgeHandler.__module__}.{_StreamingBridgeHandler.__qualname__}"
)

_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [{"name": "streaming", "source": _HANDLER_SOURCE}],
    }
}


async def test_streaming_handler_diagnostics_are_reflected_in_return_code(
    tmp_path: Path,
) -> None:
    """A handler that reports its result only via partial_result_sender.send()
    (returning None from run(), the shape of every real bridge handler in
    fine_lint/fine_inspect_code) must still produce an action result -- and
    therefore a correct return_code -- when the caller streams via a
    partial_result_token, exactly as it already does when no token is given.
    """
    sent: list[code_action.RunActionResult] = []

    def _fake_send(token, value, formats):
        sent.append(value)

    async with handler_test_session(project_dir=tmp_path, actions=_ACTIONS) as session:
        run_action_service.set_partial_result_sender(_fake_send)
        action_def = session._runner_context.project.actions[_ACTION_NAME]

        result = await run_action_service.run_action(
            action_def=action_def,
            payload=code_action.RunActionPayload(),
            meta=code_action.RunActionMeta(
                trigger=code_action.RunActionTrigger.SYSTEM,
                dev_env=code_action.DevEnv.CI,
                wal_run_id="test-run-id",
            ),
            runner_context=session._runner_context,
            partial_result_token="tok-1",
        )

    # The diagnostic really was streamed to the client (this is what shows up
    # in the CI log) ...
    assert len(sent) == 1
    assert sent[0].messages == ["file.py:1: error found"]

    # ... and must be reflected in the action's own result / return code, the
    # same way it would be if the token were absent (non-streaming callers
    # already get this right via _AccumulatingPartialResultSender).
    assert result is not None
    assert result.return_code == code_action.RunReturnCode.ERROR
