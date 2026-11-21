from __future__ import annotations

import collections.abc
import contextlib
import pathlib
import typing

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server._api_handlers import _streaming
from finecode.wm_server.runner import _internal_client_types, runner_client
from finecode.wm_server.services import partial_results_service
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed
from finecode.wm_server.services.run_service.merge_helpers import merge_slot_results


def _build_ws_context(
    tmp_path: pathlib.Path,
    *,
    client: typing.Any | None = None,
    running: bool = True,
) -> typing.Any:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    if not running:
        runner.status = runner_client.RunnerStatus.EXITED
    return wm_testing.make_workspace_context(project=project, runner=runner)


def _merge_response(
    result_by_format: _internal_client_types.ResultByFormat | None = None,
    *,
    error: str | None = None,
) -> _internal_client_types.ErMergeResultsResponse:
    return _internal_client_types.ErMergeResultsResponse(
        id=1,
        jsonrpc="2.0",
        result=_internal_client_types.ErMergeResultsResult(
            result_by_format=result_by_format,
            error=error,
        ),
    )


@pytest.mark.parametrize(
    ("entry", "formats", "expected"),
    [
        (
            {"json": {"answer": 42}, "string": "answer: 42"},
            [runner_client.RunResultFormat.STRING],
            {"string": "answer: 42"},
        ),
        (
            {"json": {"answer": 42}, "string": "answer: 42"},
            [runner_client.RunResultFormat.JSON],
            {"json": {"answer": 42}},
        ),
        (
            {"styled_text_json": {"spans": []}},
            [runner_client.RunResultFormat.STRING],
            {"styled_text_json": {"spans": []}},
        ),
    ],
)
async def test_single_entry_is_filtered_without_runner_call(
    tmp_path: pathlib.Path,
    entry: _internal_client_types.ResultByFormat,
    formats: list[runner_client.RunResultFormat],
    expected: _internal_client_types.ResultByFormat,
) -> None:
    ws_context = _build_ws_context(tmp_path)

    result = await merge_slot_results(
        project_path=tmp_path,
        action_name="test_action",
        entries=[entry],
        requested_formats=formats,
        ws_context=ws_context,
    )

    assert result == expected


async def test_no_counted_entries_returns_none(tmp_path: pathlib.Path) -> None:
    result = await merge_slot_results(
        project_path=tmp_path,
        action_name="test_action",
        entries=[{"json": {}}],
        requested_formats=[runner_client.RunResultFormat.JSON],
        ws_context=_build_ws_context(tmp_path),
    )

    assert result is None


@pytest.mark.parametrize(
    ("requested_formats", "expected_formats"),
    [
        ([runner_client.RunResultFormat.STRING], ["string", "json"]),
        ([runner_client.RunResultFormat.JSON], ["json"]),
    ],
)
async def test_multiple_entries_merge_requested_formats(
    tmp_path: pathlib.Path,
    requested_formats: list[runner_client.RunResultFormat],
    expected_formats: list[str],
) -> None:
    client = wm_testing.FakeErClient()
    merged = {"answer": 42}
    client.configure_response(_merge_response({"json": merged, "string": "answer: 42"}))
    ws_context = _build_ws_context(tmp_path, client=client)

    result = await merge_slot_results(
        project_path=tmp_path,
        action_name="test_action",
        entries=[{"json": {"answer": 20}}, {"json": {"answer": 22}}],
        requested_formats=requested_formats,
        ws_context=ws_context,
    )

    expected = (
        {"string": "answer: 42"}
        if requested_formats == [runner_client.RunResultFormat.STRING]
        else {"json": merged}
    )
    assert result == expected
    [(method, params)] = client.sent_requests
    assert method == _internal_client_types.ER_MERGE_RESULTS
    assert params.result_formats == expected_formats
    assert params.results == [{"answer": 20}, {"answer": 22}]


async def test_empty_json_does_not_drop_an_entry_with_text(
    tmp_path: pathlib.Path,
) -> None:
    result = await merge_slot_results(
        project_path=tmp_path,
        action_name="test_action",
        entries=[{"json": {}, "string": "visible"}],
        requested_formats=[runner_client.RunResultFormat.STRING],
        ws_context=_build_ws_context(tmp_path),
    )

    assert result == {"string": "visible"}


async def test_merge_failure_raises_action_run_failed(tmp_path: pathlib.Path) -> None:
    client = wm_testing.FakeErClient()
    client.configure_response(_merge_response(error="render failed"))
    ws_context = _build_ws_context(tmp_path, client=client)

    with pytest.raises(ActionRunFailed, match="render failed"):
        await merge_slot_results(
            project_path=tmp_path,
            action_name="test_action",
            entries=[{"json": {"a": 1}}, {"json": {"b": 2}}],
            requested_formats=[runner_client.RunResultFormat.STRING],
            ws_context=ws_context,
        )


async def test_no_running_runner_raises_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    ws_context = _build_ws_context(tmp_path, running=False)

    with pytest.raises(ActionRunFailed, match="no running runner"):
        await merge_slot_results(
            project_path=tmp_path,
            action_name="test_action",
            entries=[{"json": {"a": 1}}, {"json": {"b": 2}}],
            requested_formats=[runner_client.RunResultFormat.JSON],
            ws_context=ws_context,
        )


async def test_unknown_project_raises_action_run_failed(
    tmp_path: pathlib.Path,
) -> None:
    ws_context = _build_ws_context(tmp_path)

    with pytest.raises(ActionRunFailed, match="no collected actions"):
        await merge_slot_results(
            project_path=tmp_path / "missing",
            action_name="test_action",
            entries=[{"json": {"a": 1}}, {"json": {"b": 2}}],
            requested_formats=[runner_client.RunResultFormat.JSON],
            ws_context=ws_context,
        )


async def test_run_partial_results_path_returns_json_slot(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    payload = {"answer": 42}

    class _Stream:
        progress_stream: None = None

        def __aiter__(self) -> typing.AsyncIterator[dict[str, typing.Any]]:
            async def _values() -> typing.AsyncIterator[dict[str, typing.Any]]:
                yield {
                    "project": str(tmp_path),
                    "resultByFormat": {"json": payload},
                }

            return _values()

        async def final_result(self) -> dict[str, int]:
            return {"returnCode": 0}

    @contextlib.asynccontextmanager
    async def _stream_action(
        **_: typing.Any,
    ) -> collections.abc.AsyncGenerator[_Stream, None]:
        yield _Stream()

    async def _resolve_source(*_: typing.Any, **__: typing.Any) -> str:
        return "test_action"

    monkeypatch.setattr(
        partial_results_service,
        "run_action_with_partial_results",
        _stream_action,
    )
    monkeypatch.setattr(_streaming, "_resolve_source_to_name", _resolve_source)

    class _Writer:
        async def drain(self) -> None:
            return None

    result = await _streaming._handle_run_action_with_partial_results(
        {
            "actionSource": "test.actions.TestAction",
            "project": str(tmp_path),
            "partialResultToken": "token",
            "options": {"mergeResults": True},
        },
        ws_context,
        typing.cast(typing.Any, _Writer()),
    )

    assert result == {
        "returnCode": 0,
        "results": {
            str(tmp_path): {
                "test.actions.TestAction": {
                    "resultByFormat": {"json": payload},
                    "returnCode": 0,
                }
            }
        },
    }
