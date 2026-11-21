from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import typing

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers import _streaming
from finecode.wm_server.runner import _internal_client_types, runner_client
from finecode.wm_server.services import run_service
from finecode.wm_server.services.run_service import proxy_utils, run_selection
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed


class _RunContext:
    def __init__(
        self,
        partials: list[_internal_client_types.ResultByFormat],
        responses: list[runner_client.RunActionResponse],
    ) -> None:
        self.partials = partials
        self.responses = responses
        self.progress: None = None

    def __aiter__(
        self,
    ) -> collections.abc.AsyncIterator[_internal_client_types.ResultByFormat]:
        async def _values() -> collections.abc.AsyncIterator[
            _internal_client_types.ResultByFormat
        ]:
            for value in self.partials:
                yield value

        return _values()


class _Writer:
    async def drain(self) -> None:
        return None


def _make_project(path: pathlib.Path) -> domain.ResolvedProject:
    handler = domain.ActionHandler(
        name="handler",
        source="test.handlers.TestHandler",
        config={},
        env="test_env",
        dependencies=[],
    )
    action = domain.Action(
        name="test_action",
        source="test.actions.TestAction",
        handlers=[handler],
        config={},
    )
    return domain.ResolvedProject.from_collected(
        domain.CollectedProject(
            name=path.name,
            dir_path=path,
            def_path=path / "pyproject.toml",
            status=domain.ProjectStatus.CONFIG_VALID,
            env_configs={
                "test_env": domain.EnvConfig(
                    runner_config=domain.RunnerConfig(debug=False)
                )
            },
            actions=[action],
            services=[],
            action_handler_configs={},
        )
    )


async def _run_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    formats: list[str],
    partials_by_project: dict[str, list[_internal_client_types.ResultByFormat]],
    merge_results: bool = True,
    fail_project: str | None = None,
    cancel_project: str | None = None,
    workspace_scoped: bool = False,
) -> tuple[
    dict[str, typing.Any],
    list[dict[str, typing.Any]],
    list[list[runner_client.RunResultFormat]],
    list[list[_internal_client_types.ResultByFormat]],
]:
    project_paths = [tmp_path / name for name in partials_by_project]
    projects = {path: _make_project(path) for path in project_paths}
    if workspace_scoped:
        for project in projects.values():
            project.actions[0].scope = domain.ActionScope.WORKSPACE
    ws_context = context.WorkspaceContext(ws_dirs_paths=project_paths)
    ws_context.ws_projects.update(projects)
    actions_by_project = {path: ["test_action"] for path in project_paths}
    name_to_source = {"test_action": "test.actions.TestAction"}
    requested_formats: list[list[runner_client.RunResultFormat]] = []
    merge_entries: list[list[_internal_client_types.ResultByFormat]] = []
    notifications: list[dict[str, typing.Any]] = []

    async def _resolve_actions(
        *_: typing.Any, **__: typing.Any
    ) -> tuple[dict[pathlib.Path, list[str]], dict[str, str]]:
        return actions_by_project, name_to_source

    async def _noop_start(*_: typing.Any, **__: typing.Any) -> None:
        return None

    def _no_matrix(*_: typing.Any) -> dict[pathlib.Path, set[str] | None]:
        return {}

    async def _check_variants(*_: typing.Any, **__: typing.Any) -> None:
        return None

    @contextlib.asynccontextmanager
    async def _run_with_partials(
        *,
        project_dir_path: pathlib.Path,
        result_formats: list[runner_client.RunResultFormat],
        **_: typing.Any,
    ) -> collections.abc.AsyncGenerator[_RunContext, None]:
        requested_formats.append(list(result_formats))
        if project_dir_path.name == "a":
            await asyncio.sleep(0.02)
        values = partials_by_project[project_dir_path.name]
        if values:
            responses = [
                runner_client.RunActionResponse(
                    result_by_format={}, return_code=0, status="streamed"
                )
            ]
        else:
            responses = [
                runner_client.RunActionResponse(
                    result_by_format={"json": {"direct": True}, "string": "direct"},
                    return_code=0,
                    status="success",
                )
            ]
        yield _RunContext(values, responses)

    async def _merge_slot_results(
        *,
        project_path: pathlib.Path,
        entries: list[_internal_client_types.ResultByFormat],
        requested_formats: list[runner_client.RunResultFormat],
        **_: typing.Any,
    ) -> _internal_client_types.ResultByFormat:
        merge_entries.append(entries)
        if project_path.name == cancel_project:
            raise asyncio.CancelledError
        if project_path.name == fail_project:
            raise ActionRunFailed("render failed")
        if runner_client.RunResultFormat.STRING in requested_formats:
            return {"string": "merged Ω"}
        return {"json": {"merged": True}}

    def _record_notification(
        _writer: typing.Any, _method: str, payload: dict[str, typing.Any]
    ) -> None:
        notifications.append(payload)

    def _validate_selectors(*_: typing.Any) -> None:
        return None

    monkeypatch.setattr(_streaming, "_resolve_actions_by_project", _resolve_actions)
    monkeypatch.setattr(_streaming, "_notify_client", _record_notification)
    monkeypatch.setattr(_streaming, "merge_slot_results", _merge_slot_results)

    monkeypatch.setattr(run_service, "start_required_environments", _noop_start)
    monkeypatch.setattr(run_selection, "validate_run_selectors", _validate_selectors)
    monkeypatch.setattr(run_selection, "selection_for_matrixed_actions", _no_matrix)
    monkeypatch.setattr(run_selection, "check_variant_selection", _check_variants)
    monkeypatch.setattr(proxy_utils, "run_with_partial_results", _run_with_partials)

    result = await _streaming._handle_run_batch_with_partial_results(
        {
            "partialResultToken": "token",
            "actionSources": ["test.actions.TestAction"],
            "projects": [str(path) for path in project_paths],
            "options": {"resultFormats": formats, "mergeResults": merge_results},
        },
        ws_context,
        typing.cast(typing.Any, _Writer()),
    )
    return result, notifications, requested_formats, merge_entries


async def test_text_only_merge_injects_json_and_strips_notifications(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, notifications, run_formats, merge_entries = await _run_batch(
        monkeypatch,
        tmp_path,
        formats=["string"],
        partials_by_project={
            "project": [
                {"json": {"part": 1}, "string": "large π " * 100},
                {"json": {"part": 2}, "string": "second"},
            ]
        },
    )

    assert run_formats == [
        [runner_client.RunResultFormat.STRING, runner_client.RunResultFormat.JSON]
    ]
    assert [
        n["value"]["results"]["test.actions.TestAction"]["resultByFormat"]
        for n in notifications
    ] == [
        {"string": "large π " * 100},
        {"string": "second"},
    ]
    assert merge_entries == [
        [
            {"json": {"part": 1}, "string": "large π " * 100},
            {"json": {"part": 2}, "string": "second"},
        ]
    ]
    assert result["results"][str(tmp_path / "project")]["test.actions.TestAction"] == {
        "resultByFormat": {"string": "merged Ω"},
        "returnCode": 0,
    }


async def test_one_text_partial_keeps_only_the_requested_format(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, notifications, run_formats, merge_entries = await _run_batch(
        monkeypatch,
        tmp_path,
        formats=["string"],
        partials_by_project={
            "project": [{"json": {"answer": 42}, "string": "answer: 42"}]
        },
    )

    assert run_formats == [
        [runner_client.RunResultFormat.STRING, runner_client.RunResultFormat.JSON]
    ]
    assert notifications[0]["value"]["results"]["test.actions.TestAction"][
        "resultByFormat"
    ] == {"string": "answer: 42"}
    assert merge_entries == [[{"json": {"answer": 42}, "string": "answer: 42"}]]
    assert result["results"][str(tmp_path / "project")]["test.actions.TestAction"] == {
        "resultByFormat": {"string": "merged Ω"},
        "returnCode": 0,
    }


async def test_workspace_scoped_action_returns_root_slot_text(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _notifications, _run_formats, _merge_entries = await _run_batch(
        monkeypatch,
        tmp_path,
        formats=["string"],
        partials_by_project={"root": [{"json": {"answer": 42}, "string": "answer"}]},
        workspace_scoped=True,
    )

    assert result["results"][str(tmp_path / "root")]["test.actions.TestAction"] == {
        "resultByFormat": {"string": "merged Ω"},
        "returnCode": 0,
    }


async def test_direct_result_is_one_entry_and_json_only_is_unchanged(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, notifications, run_formats, merge_entries = await _run_batch(
        monkeypatch,
        tmp_path,
        formats=["json"],
        partials_by_project={"project": []},
    )

    assert run_formats == [[runner_client.RunResultFormat.JSON]]
    assert notifications[0]["value"]["results"]["test.actions.TestAction"][
        "resultByFormat"
    ] == {"json": {"direct": True}}
    assert merge_entries == [[{"json": {"direct": True}, "string": "direct"}]]
    assert result["results"][str(tmp_path / "project")]["test.actions.TestAction"] == {
        "resultByFormat": {"json": {"merged": True}},
        "returnCode": 0,
    }


async def test_text_merge_failure_is_slot_scoped_and_ordered(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _notifications, _run_formats, _merge_entries = await _run_batch(
        monkeypatch,
        tmp_path,
        formats=["string"],
        partials_by_project={
            "a": [{"json": {"a": 1}, "string": "partial a"}],
            "b": [{"json": {"b": 1}, "string": "partial b"}],
        },
        fail_project="a",
    )

    assert result["returnCode"] == 1
    assert list(result["results"]) == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert result["results"][str(tmp_path / "a")]["test.actions.TestAction"] == {
        "resultByFormat": {},
        "returnCode": 1,
    }
    assert result["results"][str(tmp_path / "b")]["test.actions.TestAction"] == {
        "resultByFormat": {"string": "merged Ω"},
        "returnCode": 0,
    }


async def test_json_only_merge_failure_still_raises(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ActionRunFailed, match="render failed"):
        await _run_batch(
            monkeypatch,
            tmp_path,
            formats=["json"],
            partials_by_project={"project": [{"json": {"item": 1}}]},
            fail_project="project",
        )


async def test_merge_cancellation_propagates(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await _run_batch(
            monkeypatch,
            tmp_path,
            formats=["string"],
            partials_by_project={"project": [{"json": {"item": 1}}]},
            cancel_project="project",
        )
