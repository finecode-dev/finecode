"""ADR-0082 rule 1 — a run dispatched from a request handler that still holds
its caller's connection is addressed to that connection.

Driven over the real TCP loopback dispatch loop (``tests/integration/conftest.py``),
so what is exercised is the whole request-handler-side chain — request parsing,
the streaming handler, the executor, ``in_flight_runs.track`` and ``bind_run`` —
rather than any one hop of it. A hop that dropped ``origin`` would leave a
connected client unaddressable while looking exactly like a run that genuinely
had nobody to ask, which is the failure these tests exist to catch.
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import elicitation_bridge, runner_client
from finecode.wm_server.services import in_flight_runs, run_service
from finecode.wm_server.services.run_service import proxy_utils

_ACTION_SOURCE = "pkg.Lint"


def _seed_project(wm_client, project_dir: pathlib.Path) -> None:
    ws_context = wm_client.ws_context
    project = wm_testing.make_single_action_project(
        dir_path=project_dir, action_name="lint", action_source=_ACTION_SOURCE
    )
    project.actions[0].canonical_source = _ACTION_SOURCE
    ws_context.ws_projects[project_dir] = project
    ws_context.ws_projects_raw_configs[project_dir] = {"tool": {"finecode": {}}}
    ws_context.ws_projects_extension_runners[project_dir] = {
        "test_env": wm_testing.make_running_runner(
            working_dir_path=project_dir, client=wm_testing.FakeErClient()
        )
    }


@pytest.fixture
def bound_connections(monkeypatch: pytest.MonkeyPatch, wm_client):
    """Who the run in flight is addressed to, sampled while it is dispatching.

    Sampled from inside the dispatch because that is the only moment the answer
    exists: ``bind_run`` releases the entry as soon as the run ends, exactly so a
    later question is never answered with a client that has moved on.

    Both dispatch seams are sampled — the plain one and the streaming one — so
    one list covers every path a request handler can take.
    """
    ws_context = wm_client.ws_context
    seen: list[object] = []

    def _sample() -> None:
        for project_path in list(ws_context.in_flight_runs):
            for run in in_flight_runs.runs_in_project(ws_context, project_path):
                seen.append(elicitation_bridge.originating_client_for_run(run.run_id))

    async def _execute_action(**kwargs):
        _sample()
        return runner_client.RunActionResponse(result_by_format={}, return_code=0)

    async def _run_action_and_notify(**kwargs):
        _sample()
        # "streamed" is what an ER reports when it delivered its result as
        # partials rather than in the response body — the shape this path
        # requires when `result_by_format` is empty.
        return runner_client.RunActionResponse(
            result_by_format={}, return_code=0, status="streamed"
        )

    async def _get_partial_results(result_list, partial_result_token, runner):
        return None

    async def _start_required_environments(*args, **kwargs):
        return None

    monkeypatch.setattr(proxy_utils, "_execute_action", _execute_action)
    monkeypatch.setattr(proxy_utils, "run_action_and_notify", _run_action_and_notify)
    monkeypatch.setattr(proxy_utils, "get_partial_results", _get_partial_results)
    monkeypatch.setattr(
        run_service, "start_required_environments", _start_required_environments
    )
    return seen


async def test_a_run_with_a_progress_token_is_addressed_to_its_caller(
    wm_client, bound_connections, tmp_path
) -> None:
    """``actions/run`` with a ``progressToken`` forwards progress to this
    connection for the whole run, so the connection is just as much the run's
    origin as on the partial-results path — an ER that elicits has a client to
    ask.
    """
    _seed_project(wm_client, tmp_path)

    await wm_client.request(
        "actions/run",
        {
            "actionSource": _ACTION_SOURCE,
            "project": str(tmp_path),
            "params": {},
            "progressToken": "token-1",
        },
    )

    assert bound_connections and all(c is not None for c in bound_connections)


async def test_a_batch_with_a_progress_token_is_addressed_to_its_caller(
    wm_client, bound_connections, tmp_path
) -> None:
    """Every run in the batch, not just the first: aggregated progress goes to
    this one connection until the batch ends.
    """
    _seed_project(wm_client, tmp_path)

    await wm_client.request(
        "actions/runBatch",
        {
            "actionSources": [_ACTION_SOURCE],
            "projects": [str(tmp_path)],
            "params": {},
            "progressToken": "token-1",
        },
    )

    assert bound_connections and all(c is not None for c in bound_connections)


async def test_a_run_with_a_partial_result_token_is_addressed_to_its_caller(
    wm_client, bound_connections, tmp_path
) -> None:
    """The chain the streamed partial-results handler dispatches through —
    ``partial_results_service`` → ``run_with_partial_results`` → ``track`` →
    ``bind_run`` — carries the origin the whole way.
    """
    _seed_project(wm_client, tmp_path)

    await wm_client.request(
        "actions/run",
        {
            "actionSource": _ACTION_SOURCE,
            "project": str(tmp_path),
            "params": {},
            "partialResultToken": "token-1",
        },
    )

    assert bound_connections and all(c is not None for c in bound_connections)


async def test_a_run_with_no_streamed_connection_is_addressed_to_nobody(
    wm_client, bound_connections, tmp_path
) -> None:
    """The other half of the property, and the reason ``None`` stays a real
    value: a plain request/response handler never receives the caller's writer,
    so it has nothing to point an ER at and says so rather than guessing.
    """
    _seed_project(wm_client, tmp_path)

    await wm_client.request(
        "actions/run",
        {"actionSource": _ACTION_SOURCE, "project": str(tmp_path), "params": {}},
    )

    assert bound_connections == [None]
