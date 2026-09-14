"""Back-channel project dispatches never wait for the process budget (ADR-0094).

A streaming child arrives at the WM at orchestration depth 0 (the streaming
path forwards no ``orchestrationDepth``), so if the run that asked for it holds
budget slots a waiting child would deadlock on them. Declaring ``waits=False``
is what gives it the one slot it needs.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from finecode.wm_server import domain
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_client,
)
from finecode.wm_server.services.run_service import er_dispatch
from finecode.wm_server.testing import make_running_runner

_CANONICAL_SOURCE = "test.actions.TestAction"


class _FakePartialContext:
    def __init__(self) -> None:
        self.responses: list = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run_action(self, **kwargs):
        self.calls.append(("run_action", kwargs))
        return runner_client.RunActionResponse(
            result_by_format={"json": {}}, return_code=0
        )

    @contextlib.asynccontextmanager
    async def run_action_with_partial_results(self, **kwargs):
        self.calls.append(("run_action_with_partial_results", kwargs))
        yield _FakePartialContext()


def _make_params(
    *, partial_result_token: int | str | None = None
) -> _internal_client_types.RunActionInProjectParams:
    return _internal_client_types.RunActionInProjectParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        partial_result_token=partial_result_token,
    )


def _origin() -> elicitation_bridge.RunDispatchOrigin:
    return elicitation_bridge.RunDispatchOrigin(connection=None)


async def test_streaming_back_channel_dispatch_never_waits(tmp_path: Path) -> None:
    """A streaming child whose parent holds slots must still start.

    If it waited, the parent would hold slots while waiting on a child that can
    never spawn anything — the deadlock the budget exists to prevent.
    """
    executor = _FakeExecutor()
    runner = make_running_runner(working_dir_path=tmp_path)

    await er_dispatch._BridgeHandlers()._run_action_in_project(
        runner, _make_params(partial_result_token=1), executor, _origin()
    )

    kind, kwargs = executor.calls[0]
    assert kind == "run_action_with_partial_results"
    assert kwargs["budget"] == domain.RunBudget(waits=False)


async def test_plain_back_channel_dispatch_never_waits(tmp_path: Path) -> None:
    """A plain back-channel dispatch gets the same never-waiting budget."""
    executor = _FakeExecutor()
    runner = make_running_runner(working_dir_path=tmp_path)

    await er_dispatch._BridgeHandlers()._run_action_in_project(
        runner, _make_params(), executor, _origin()
    )

    kind, kwargs = executor.calls[0]
    assert kind == "run_action"
    assert kwargs["budget"] == domain.RunBudget(waits=False)
