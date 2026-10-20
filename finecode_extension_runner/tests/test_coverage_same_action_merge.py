"""Same-action merging joins coverage.

All merging of two results of the *same* action goes through ``update()``, and
the join is installed into every ``update()`` by ``__init_subclass__`` — so
the framework's merge sites (sequential handlers, the partial-result
coalescing buffer, ``actions/mergeResults``) need no rewiring, only tests that
prove coverage survives each one. These tests are the proof of that claim:
the ``format_file`` dispatch→save shape, the 300 ms debounce coalescing, the
cross-env merge service, the coverage-only partial's correct type, and the
``current_result`` aliasing surviving the merge.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri

from finecode_extension_runner import partial_result_sender as prs_module
from finecode_extension_runner._services import merge_results as merge_results_service
from finecode_extension_runner.testing import handler_test_session

_MISS_URI = ResourceUri("file:///input.py")


@dataclasses.dataclass
class _MergeRunResult(code_action.RunActionResult):
    """Accumulates per-key lists, like the real dispatch results."""

    values: dict[str, list[str]]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, _MergeRunResult):
            return
        for key, values in other.values.items():
            self.values.setdefault(key, []).extend(values)


class _AddContext(code_action.RunActionContext[code_action.RunActionPayload]):
    observed_current_result_ids: list[int] = []


class _AddAction(
    code_action.Action[code_action.RunActionPayload, _AddContext, _MergeRunResult]
):
    PAYLOAD_TYPE = code_action.RunActionPayload
    RUN_CONTEXT_TYPE = _AddContext
    RESULT_TYPE = _MergeRunResult


class _MissHandler(
    code_action.ActionHandler[_AddAction, code_action.ActionHandlerConfig]
):
    """The dispatch step: reports that no subaction covered the input."""

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _AddContext,
    ) -> _MergeRunResult:
        return _MergeRunResult(
            values={},
            coverage=[
                ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
            ],
        )


class _PlainHandler(
    code_action.ActionHandler[_AddAction, code_action.ActionHandlerConfig]
):
    """The follow-on step (``format_file``'s ``save``): a plain result."""

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _AddContext,
    ) -> _MergeRunResult:
        return _MergeRunResult(values={"items": ["done"]})


class _IdentityRecordingHandler(
    code_action.ActionHandler[_AddAction, code_action.ActionHandlerConfig]
):
    """Second sequential handler: records the object ``current_result`` aliases
    after the first handler's merge, so the test can assert the final result is
    that same object."""

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: _AddContext,
    ) -> _MergeRunResult:
        current = run_context.current_result
        assert current is not None and current.unhandled == [
            ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
        ]
        _AddContext.observed_current_result_ids.append(id(current))
        return _MergeRunResult(values={"items": ["done"]})


_ACTION_NAME = _AddAction.__name__
_ACTION_SOURCE = f"{_AddAction.__module__}.{_AddAction.__qualname__}"
_MISS_SOURCE = f"{_MissHandler.__module__}.{_MissHandler.__qualname__}"
_PLAIN_SOURCE = f"{_PlainHandler.__module__}.{_PlainHandler.__qualname__}"
_IDENTITY_SOURCE = (
    f"{_IdentityRecordingHandler.__module__}.{_IdentityRecordingHandler.__qualname__}"
)

_TWO_HANDLERS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [
            {"name": "dispatch", "source": _MISS_SOURCE},
            {"name": "followup", "source": _PLAIN_SOURCE},
        ],
    }
}


async def test_sequential_handlers_keep_the_miss(tmp_path: Path) -> None:
    """The ``format_file`` dispatch→save shape: the dispatcher reports a miss
    and a second handler returns a plain result; the final result must still
    report the miss."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_TWO_HANDLERS
    ) as session:
        result = await session.run_action(_ACTION_NAME)
    assert isinstance(result, _MergeRunResult)
    assert result.values == {"items": ["done"]}
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
    ]


async def test_debounce_coalescing_keeps_coverage(tmp_path: Path) -> None:
    """Two ``send()`` calls inside the 300 ms debounce window collapse into one
    coalesced partial (``PartialResultSender`` merges by token); the miss from
    the first call must survive into the coalesced value — this is the common
    IDE path where a dispatch streams a miss then a plain result."""
    sent: list[code_action.RunActionResult] = []

    def _sender(token, value, formats=None) -> None:
        sent.append(value)

    coalescer = prs_module.PartialResultSender(sender=_sender, wait_time_ms=300)
    first = _MergeRunResult(
        values={},
        coverage=[ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)],
    )
    second = _MergeRunResult(values={"items": ["done"]})
    await coalescer.schedule_sending("tok", first, ["json"])
    await coalescer.schedule_sending("tok", second, ["json"])
    await coalescer.send_all_immediately()

    assert len(sent) == 1
    merged = sent[0]
    assert isinstance(merged, _MergeRunResult)
    assert merged.values == {"items": ["done"]}
    assert merged.coverage == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
    ]


async def test_merge_results_service_keeps_coverage(tmp_path: Path) -> None:
    """``actions/mergeResults`` (cross-env partial merge) goes through the same
    ``update()`` join, so a serialized partial carrying a miss still carries it
    after the merge — the WM-facing boundary for streamed partials."""
    async with handler_test_session(
        project_dir=tmp_path, actions=_TWO_HANDLERS
    ) as session:
        with_miss = dataclasses.asdict(
            _MergeRunResult(
                values={"a": ["1"]},
                coverage=[
                    ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
                ],
            )
        )
        plain = dataclasses.asdict(_MergeRunResult(values={"b": ["2"]}))
        merged = await merge_results_service.merge_results(
            action_name=_ACTION_NAME,
            results=[with_miss, plain],
            runner_context=session._runner_context,
        )
    assert merged["values"] == {"a": ["1"], "b": ["2"]}
    assert merged["coverage"] == [
        dataclasses.asdict(
            ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
        )
    ]


def test_coverage_only_partial_is_typed_and_mergable() -> None:
    """A coverage-only partial is an instance of the action's own
    ``RESULT_TYPE`` with empty domain data — never a bare ``RunActionResult``,
    whose ``update()`` raises ``NotImplementedError``. Sending one first must
    not break the merge that follows it."""
    coverage_only = _MergeRunResult(
        values={},
        coverage=[ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)],
    )
    assert isinstance(coverage_only, _MergeRunResult)
    with_domain = _MergeRunResult(values={"a": ["1"]})
    coverage_only.update(with_domain)
    assert coverage_only.values == {"a": ["1"]}
    assert coverage_only.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_MISS_URI)
    ]


async def test_current_result_aliasing_survives_the_merge(tmp_path: Path) -> None:
    """After the first sequential handler completes, ``current_result`` aliases
    the accumulating object; a later handler merging into it must not break
    that aliasing (run_action.py's mutable-aggregate invariant), so the final
    returned result is the very object a mid-run handler saw as current."""
    _AddContext.observed_current_result_ids.clear()
    actions = {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": [
                {"name": "dispatch", "source": _MISS_SOURCE},
                {"name": "identity", "source": _IDENTITY_SOURCE},
            ],
        }
    }
    async with handler_test_session(project_dir=tmp_path, actions=actions) as session:
        result = await session.run_action(_ACTION_NAME)
    assert isinstance(result, _MergeRunResult)
    assert _AddContext.observed_current_result_ids == [id(result)]
