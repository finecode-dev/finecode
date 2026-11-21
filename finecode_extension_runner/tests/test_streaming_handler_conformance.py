from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_runner import partial_result_sender
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _AppendResult(code_action.RunActionResult):
    text: str = ""

    def update(self, other: code_action.RunActionResult) -> None:
        if isinstance(other, _AppendResult):
            self.text += other.text


class _Payload(code_action.RunActionPayload): ...


class _SchedulerPayload(code_action.RunActionPayload):
    def __aiter__(self) -> AsyncIterator[str]:
        async def _items() -> AsyncIterator[str]:
            yield "item"

        return _items()


class _Context(code_action.RunActionWithPartialResultsContext[_Payload]): ...


class _SchedulerContext(
    code_action.RunActionWithPartialResultsContext[_SchedulerPayload]
): ...


class _Action(code_action.Action[_Payload, _Context, _AppendResult]):
    PAYLOAD_TYPE = _Payload
    RUN_CONTEXT_TYPE = _Context
    RESULT_TYPE = _AppendResult


class _SchedulerAction(
    code_action.Action[_SchedulerPayload, _SchedulerContext, _AppendResult]
):
    PAYLOAD_TYPE = _SchedulerPayload
    RUN_CONTEXT_TYPE = _SchedulerContext
    RESULT_TYPE = _AppendResult


class _ChildAction(code_action.Action[_Payload, _Context, _AppendResult]):
    PAYLOAD_TYPE = _Payload
    RUN_CONTEXT_TYPE = _Context
    RESULT_TYPE = _AppendResult


class _ParentAction(code_action.Action[_Payload, _Context, _AppendResult]):
    PAYLOAD_TYPE = _Payload
    RUN_CONTEXT_TYPE = _Context
    RESULT_TYPE = _AppendResult


async def _generator_handler(
    payload: _Payload, run_context: _Context
) -> AsyncIterator[_AppendResult]:
    _ = payload, run_context
    yield _AppendResult(text="a")
    yield _AppendResult(text="b")


async def _send_and_return_handler(
    payload: _Payload, run_context: _Context
) -> _AppendResult:
    _ = payload
    result = _AppendResult(text="same")
    await run_context.partial_result_sender.send(result)
    return result


async def _send_only_handler(payload: _Payload, run_context: _Context) -> None:
    _ = payload
    await run_context.partial_result_sender.send(_AppendResult(text="sent"))


async def _scheduler_handler(
    payload: _SchedulerPayload, run_context: _SchedulerContext
) -> None:
    async def _result() -> _AppendResult:
        return _AppendResult(text="scheduled")

    async for item in payload:
        run_context.partial_result_scheduler.schedule(item, _result())


async def _parent_handler(
    payload: _Payload,
    run_context: _Context,
    action_runner: iprojectactionrunner.IProjectActionRunner,
) -> _AppendResult:
    _ = payload
    return await action_runner.run_action(
        iprojectactionrunner.ActionRef.from_type(_ChildAction),
        _Payload(),
        run_context.meta,
    )


class _LocalActionRunner:
    def __init__(self) -> None:
        self.session: Any = None

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: code_action.RunActionPayload,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> _AppendResult:
        _ = action_type, payload, meta, caller_kwargs
        return cast(
            _AppendResult,
            await self.session.run_action(_ChildAction.__name__, _Payload()),
        )


_HANDLERS = (
    _generator_handler,
    _send_and_return_handler,
    _send_only_handler,
    _scheduler_handler,
)


def _action_config(
    action: type[code_action.Action[Any, Any, Any]], handler: Callable[..., Any]
) -> dict[str, dict[str, Any]]:
    action_name = action.__name__
    return {
        action_name: {
            "source": f"{action.__module__}.{action.__qualname__}",
            "handlers": [
                {"name": "handler", "source": f"{handler.__module__}.{handler.__qualname__}"}
            ],
        }
    }


async def _run(
    tmp_path: Path,
    handler: Callable[..., Any],
    action: type[code_action.Action[Any, Any, Any]],
    payload: code_action.RunActionPayload,
    token: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_AppendResult | None, list[code_action.RunActionResult]]:
    monkeypatch.setattr(
        run_action_service,
        "partial_result_sender",
        partial_result_sender.PartialResultSender(
            sender=_discard_send,
            wait_time_ms=300,
        ),
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_action_config(action, handler),
    ) as session:
        result = await session.run_action(
            action.__name__, payload, partial_result_token=token
        )
        return cast(_AppendResult | None, result), session.partial_results.events


async def test_handler_shapes_have_equal_token_and_no_token_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index, handler in enumerate(_HANDLERS):
        action = _SchedulerAction if handler is _scheduler_handler else _Action
        payload = _SchedulerPayload() if handler is _scheduler_handler else _Payload()
        token_result, partials = await _run(
            tmp_path / str(index), handler, action, payload, "tok", monkeypatch
        )
        plain_result, _ = await _run(
            tmp_path / str(index), handler, action, payload, None, monkeypatch
        )

        expected = _AppendResult()
        if handler is _generator_handler:
            for text in ("a", "b"):
                expected.update(_AppendResult(text=text))
        elif handler is _scheduler_handler:
            expected.update(_AppendResult(text="scheduled"))
        else:
            for partial in partials:
                expected.update(partial)

        assert token_result == plain_result, handler.__name__
        assert token_result == expected, handler.__name__


async def test_run_action_parent_receives_child_generator_final_result(
    tmp_path: Path,
) -> None:
    action_runner = _LocalActionRunner()
    actions = {
        _ChildAction.__name__: {
            "source": f"{_ChildAction.__module__}.{_ChildAction.__qualname__}",
            "handlers": [
                {
                    "name": "generator",
                    "source": f"{_generator_handler.__module__}.{_generator_handler.__qualname__}",
                }
            ],
        },
        _ParentAction.__name__: {
            "source": f"{_ParentAction.__module__}.{_ParentAction.__qualname__}",
            "handlers": [
                {
                    "name": "parent",
                    "source": f"{_parent_handler.__module__}.{_parent_handler.__qualname__}",
                }
            ],
        },
    }
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides={iprojectactionrunner.IProjectActionRunner: action_runner},
    ) as session:
        action_runner.session = session
        result = await session.run_action(_ParentAction.__name__, _Payload())

    assert result == _AppendResult(text="ab")


def _discard_send(
    _token: int | str,
    _value: code_action.RunActionResult,
    _formats: list[str] | None,
) -> None:
    return None
