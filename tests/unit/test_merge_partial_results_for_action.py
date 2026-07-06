from __future__ import annotations

import pathlib

import pytest

from finecode.wm_server import domain, testing as wm_testing
from finecode.wm_server._api_handlers._helpers import _merge_partial_results_for_action
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed


def _build_ws_context(tmp_path: pathlib.Path, *, client=None, running: bool = True):
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    if not running:
        runner.status = domain.ExtensionRunnerStatus.EXITED
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return ws_context


async def test_no_payloads_returns_none(tmp_path: pathlib.Path) -> None:
    """No streamed data for this slot: nothing to merge, no runner call needed."""
    ws_context = _build_ws_context(tmp_path)

    result = await _merge_partial_results_for_action(
        project_path=tmp_path,
        action_name="test_action",
        json_payloads=[None, {}],
        ws_context=ws_context,
    )

    assert result is None


async def test_single_payload_passes_through_without_runner_call(
    tmp_path: pathlib.Path,
) -> None:
    """A single partial needs no merge — returned as-is, and the ER is never
    asked, since ``FakeErClient`` would raise if called unconfigured."""
    ws_context = _build_ws_context(tmp_path)
    payload = {"messages": {"file://a": [1]}}

    result = await _merge_partial_results_for_action(
        project_path=tmp_path,
        action_name="test_action",
        json_payloads=[payload],
        ws_context=ws_context,
    )

    assert result is payload


async def test_multiple_payloads_merge_via_er_merge_results(
    tmp_path: pathlib.Path,
) -> None:
    """Two-or-more partials for the same slot are merged through the ER's
    ``actions/mergeResults`` command — the same primitive ``proxy_utils`` uses
    for multi-env merges — since the WM never holds the typed ``RESULT_TYPE``
    needed to call ``.update()`` itself.
    """
    client = wm_testing.FakeErClient()
    merged = {"messages": {"file://a": [1], "file://b": [2]}}
    client.configure_response(
        _internal_client_types.ErMergeResultsResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErMergeResultsResult(merged=merged),
        )
    )
    ws_context = _build_ws_context(tmp_path, client=client)
    payload_a = {"messages": {"file://a": [1]}}
    payload_b = {"messages": {"file://b": [2]}}

    result = await _merge_partial_results_for_action(
        project_path=tmp_path,
        action_name="test_action",
        json_payloads=[payload_a, payload_b],
        ws_context=ws_context,
    )

    assert result == merged
    [(method, params)] = client.sent_requests
    assert method == _internal_client_types.ER_MERGE_RESULTS
    assert params.action_name == "test_action"
    assert params.results == [payload_a, payload_b]


async def test_merge_rpc_error_raises_action_run_failed(tmp_path: pathlib.Path) -> None:
    """The caller opted into ``mergeResults`` to get the complete result, so a
    failed merge must be a loud error — not a silent fallback to a single
    partial, which would re-introduce the data loss this path exists to fix.
    """
    client = wm_testing.FakeErClient()
    client.configure_response(
        _internal_client_types.ErMergeResultsResponse(
            id=1,
            jsonrpc="2.0",
            result=_internal_client_types.ErMergeResultsResult(error="boom"),
        )
    )
    ws_context = _build_ws_context(tmp_path, client=client)

    with pytest.raises(ActionRunFailed):
        await _merge_partial_results_for_action(
            project_path=tmp_path,
            action_name="test_action",
            json_payloads=[{"messages": {}}, {"messages": {}}],
            ws_context=ws_context,
        )


async def test_no_running_runner_raises_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    """Merging is only possible on a running ER; if none is available for any
    of the action's handler envs, fail loudly rather than guess."""
    ws_context = _build_ws_context(tmp_path, running=False)

    with pytest.raises(ActionRunFailed, match="no running runner"):
        await _merge_partial_results_for_action(
            project_path=tmp_path,
            action_name="test_action",
            json_payloads=[{"messages": {}}, {"messages": {}}],
            ws_context=ws_context,
        )


async def test_unknown_project_raises_action_run_failed(tmp_path: pathlib.Path) -> None:
    """A project path with no collected actions (e.g. never resolved) can't be
    merged against — fail loudly instead of guessing which partial to keep."""
    ws_context = wm_testing.make_workspace_context(
        project=wm_testing.make_single_action_project(
            dir_path=tmp_path, action_name="test_action"
        ),
        runner=wm_testing.make_running_runner(working_dir_path=tmp_path),
    )
    unknown_path = tmp_path / "does-not-exist"

    with pytest.raises(ActionRunFailed):
        await _merge_partial_results_for_action(
            project_path=unknown_path,
            action_name="test_action",
            json_payloads=[{"messages": {}}, {"messages": {}}],
            ws_context=ws_context,
        )
