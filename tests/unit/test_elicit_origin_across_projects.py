"""A question asked from inside a nested run still reaches the person who
started the outer one (ADR-0082 rule 1).

A handler whose own work is another action leaves the WM dispatching a *new*
run, in a possibly different project, on a possibly different ER. That run is a
continuation of the same work and belongs to the same person — but nothing about
the runner that ends up asking says so, and addressing by project cannot say it
either, since two clients may be running the same project at once. So the
calling run's identity is what travels, and the nested run inherits its client
from it.
"""

from __future__ import annotations

import pathlib
import typing

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, elicitation_bridge
from finecode.wm_server.services.run_service import er_dispatch

_CANONICAL_SOURCE = "test.actions.TestAction"
_OUTER_RUN = "run-outer"


@pytest.fixture(autouse=True)
def clean_origins():
    yield
    elicitation_bridge.reset_origins()


class _AskingErClient(wm_testing.FakeErClient):
    """Records who could have been asked, as the ER itself would resolve it.

    The ER names the run the WM gave it in this very request, which is exactly
    what ``handle_elicit`` looks the connection up by.
    """

    def __init__(self) -> None:
        super().__init__()
        self.origin_while_running: list[object] = []
        self.run_ids: list[str | None] = []

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
    ) -> typing.Any:
        run_id = (getattr(params, "options", None) or {}).get("runId")
        self.run_ids.append(run_id)
        self.origin_while_running.append(
            elicitation_bridge.originating_client_for_run(run_id)
        )
        return await super().send_request(method, params, timeout)


class _DispatchingErClient(_AskingErClient):
    """An ER that dispatches again while running, one hop deeper."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatch_onward: typing.Callable | None = None

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
    ) -> typing.Any:
        result = await super().send_request(method, params, timeout)
        if self.dispatch_onward is not None:
            # The ER echoes the run it is executing, which is how the WM knows
            # whose continuation the next run is.
            await self.dispatch_onward(self.run_ids[-1])
        return result


def _project_with_runner(dir_path: pathlib.Path, client: _AskingErClient | None = None):
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    er_client = client if client is not None else _AskingErClient()
    er_client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    runner = wm_testing.make_running_runner(working_dir_path=dir_path, client=er_client)
    return project, runner, er_client


def _params(project_paths: list[str], run_id: str | None):
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        project_paths=project_paths,
        run_id=run_id,
    )


def _workspace(*projects) -> object:
    first_project, first_runner = projects[0]
    ws_context = wm_testing.make_workspace_context(
        project=first_project, runner=first_runner
    )
    for project, runner in projects[1:]:
        ws_context.ws_projects[project.dir_path] = project
        ws_context.ws_projects_extension_runners[project.dir_path] = {
            "test_env": runner
        }
    return ws_context


async def test_the_origin_reaches_the_project_the_run_fanned_into(
    tmp_path: pathlib.Path,
) -> None:
    started_in = tmp_path / "started_in"
    fanned_into = tmp_path / "fanned_into"
    project, runner, _ = _project_with_runner(started_in)
    other_project, other_runner, other_client = _project_with_runner(fanned_into)
    ws_context = _workspace((project, runner), (other_project, other_runner))

    terminal = object()
    with (
        elicitation_bridge.originating_client(terminal),
        elicitation_bridge.bind_run(_OUTER_RUN),
    ):
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_params([str(fanned_into)], _OUTER_RUN),
            ws_context=ws_context,
        )

    assert other_client.origin_while_running == [terminal]
    # The nested run got an id of its own rather than borrowing the outer one.
    assert other_client.run_ids != [_OUTER_RUN]
    # Bound for the nested run only: it is over, and so is the loan.
    assert (
        elicitation_bridge.originating_client_for_run(other_client.run_ids[0]) is None
    )


async def test_an_unattended_run_does_not_acquire_an_origin_on_the_way(
    tmp_path: pathlib.Path,
) -> None:
    """Nothing is invented for a run nobody is watching."""
    started_in = tmp_path / "started_in"
    fanned_into = tmp_path / "fanned_into"
    project, runner, _ = _project_with_runner(started_in)
    other_project, other_runner, other_client = _project_with_runner(fanned_into)
    ws_context = _workspace((project, runner), (other_project, other_runner))

    await er_dispatch._BridgeHandlers().run_action_in_workspace(
        runner=runner,
        params=_params([str(fanned_into)], _OUTER_RUN),
        ws_context=ws_context,
    )

    assert other_client.origin_while_running == [None]


async def test_a_second_client_running_the_same_project_is_not_confused_for_the_first(
    tmp_path: pathlib.Path,
) -> None:
    """What addressing by project could not do.

    Both runs reach the same project's ER; only the run each one names says
    whose question it is.
    """
    started_in = tmp_path / "started_in"
    shared = tmp_path / "shared"
    project, runner, _ = _project_with_runner(started_in)
    shared_project, shared_runner, shared_client = _project_with_runner(shared)
    ws_context = _workspace((project, runner), (shared_project, shared_runner))

    first_terminal = object()
    second_terminal = object()
    for run_id, terminal in (("run-a", first_terminal), ("run-b", second_terminal)):
        with (
            elicitation_bridge.originating_client(terminal),
            elicitation_bridge.bind_run(run_id),
        ):
            await er_dispatch._BridgeHandlers().run_action_in_workspace(
                runner=runner,
                params=_params([str(shared)], run_id),
                ws_context=ws_context,
            )

    assert shared_client.origin_while_running == [first_terminal, second_terminal]


async def test_the_origin_survives_a_second_hop(tmp_path: pathlib.Path) -> None:
    """A handler that dispatches from inside a dispatch is still asking for the
    same person.

    Each hop inherits from the run named in the call, and the nested run is
    bound to the same client — so depth needs no special handling and no hop has
    to know how deep it is.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"

    project, runner, _ = _project_with_runner(first)
    second_client = _DispatchingErClient()
    second_project, second_runner, _ = _project_with_runner(second, second_client)
    third_project, third_runner, third_client = _project_with_runner(third)
    ws_context = _workspace(
        (project, runner),
        (second_project, second_runner),
        (third_project, third_runner),
    )

    async def _second_hop(calling_run_id: str | None) -> None:
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=second_runner,
            params=_params([str(third)], calling_run_id),
            ws_context=ws_context,
        )

    second_client.dispatch_onward = _second_hop

    terminal = object()
    with (
        elicitation_bridge.originating_client(terminal),
        elicitation_bridge.bind_run(_OUTER_RUN),
    ):
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner,
            params=_params([str(second)], _OUTER_RUN),
            ws_context=ws_context,
        )

    assert second_client.origin_while_running == [terminal]
    assert third_client.origin_while_running == [terminal]
    # Three distinct runs, one client.
    assert len({_OUTER_RUN, second_client.run_ids[0], third_client.run_ids[0]}) == 3
