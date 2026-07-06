from __future__ import annotations

import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_runner.testing import handler_test_session


@dataclasses.dataclass
class _Hint:
    """Nested dataclass field — the shape that requires depth conversion."""

    label: str


@dataclasses.dataclass
class _HintsRunResult(code_action.RunActionResult):
    hints: list[_Hint] = dataclasses.field(default_factory=list)

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _HintsRunResult):
            return
        self.hints.extend(other.hints)


class _HintsTestAction(
    code_action.Action[
        code_action.RunActionPayload, code_action.RunActionContext, _HintsRunResult
    ]
):
    # HANDLER_EXECUTION defaults to SEQUENTIAL — matches the multi-env segment
    # path exercised by run_handlers_raw's previous_result reconstruction.
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = code_action.RunActionContext
    RESULT_TYPE = _HintsRunResult


class _SecondSegmentHandler(
    code_action.ActionHandler[_HintsTestAction, code_action.ActionHandlerConfig]
):
    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext,
    ) -> _HintsRunResult:
        return _HintsRunResult(hints=[_Hint(label="b")])


_ACTION_NAME = _HintsTestAction.__name__
_ACTION_SOURCE = f"{_HintsTestAction.__module__}.{_HintsTestAction.__qualname__}"
_SECOND_SOURCE = (
    f"{_SecondSegmentHandler.__module__}.{_SecondSegmentHandler.__qualname__}"
)

_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [
            # Only "second" is requested per run_handlers call below — this
            # mirrors a multi-env sequential run where an earlier segment
            # (whose result is handed in as previous_result) already ran in
            # a different env/process.
            {"name": "second", "source": _SECOND_SOURCE},
        ],
    }
}


async def test_previous_result_with_nested_field_is_converted_in_depth(
    tmp_path: Path,
) -> None:
    """Regression test: run_handlers_raw must convert previous_result in depth,
    not only at the top level.

    In a multi-env sequential run, the WM hands the prior segment's serialized
    result back to the next segment as ``previous_result`` (a plain dict).
    run_handlers_raw used to reconstruct it via
    ``action_exec_info.result_type(**request.previous_result)`` — a raw
    dataclass constructor call that only assigns top-level fields verbatim
    without recursing into nested ones. A field like ``hints: list[_Hint]``
    stayed a list of plain dicts instead of ``_Hint`` instances. Once a later
    handler's result got merged in via ``update()``, those unconverted nested
    values survived into the final ``cattrs.unstructure()`` call and crashed
    with an ``AttributeError`` when it tried to read an attribute off a dict.
    """
    async with handler_test_session(project_dir=tmp_path, actions=_ACTIONS) as session:
        response = await session.run_handlers(
            _ACTION_NAME,
            ["second"],
            previous_result={"hints": [{"label": "a"}]},
        )

    assert response.result["hints"] == [{"label": "a"}, {"label": "b"}]
