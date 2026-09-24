from __future__ import annotations

import typing
from pathlib import Path

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._actions import _handle_get_payload_schemas
from finecode.wm_server.runner import runner_client, runner_manager
from finecode.wm_server.services import run_service
from finecode.wm_server.testing import (
    make_initializing_runner,
    make_multi_env_action_project,
    make_running_runner,
    make_single_action_project,
    make_workspace_context,
)

_ACTION_SOURCE = "test.actions.TestAction"
_ACTION_NAME = "test_action"
_HANDLER_ENV = "testing@cpython-3.14"


def _fake_get_payload_schemas(schemas: dict):
    async def _fake(_runner: runner_client.ExtensionRunnerInfo) -> dict:
        return schemas

    return _fake


def _make_ws_context(
    tmp_path: Path,
) -> tuple[domain.ResolvedProject, context.WorkspaceContext]:
    project = make_single_action_project(
        dir_path=tmp_path, action_name=_ACTION_NAME, handler_env=_HANDLER_ENV
    )
    ws_context = make_workspace_context(
        project=project,
        runner=make_initializing_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    return project, ws_context


async def test_start_runners_starts_envs_and_returns_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that cannot proceed without the schema asks the WM to start the
    handler envs, then gets the schema. Without the gate the call returns None
    whenever no handler-env runner happens to be running yet.
    """
    project, ws_context = _make_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )
    recorded: list[dict[Path, list[str]]] = []

    async def fake_start_required_environments(
        actions_by_projects: dict[Path, list[str]],
        ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        recorded.append(actions_by_projects)
        ws_context.ws_projects_extension_runners[project.dir_path][_HANDLER_ENV] = (
            make_running_runner(working_dir_path=tmp_path, env_name=_HANDLER_ENV)
        )

    monkeypatch.setattr(
        run_service, "start_required_environments", fake_start_required_environments
    )

    result = await _handle_get_payload_schemas(
        {
            "project": str(tmp_path),
            "actionSources": [_ACTION_SOURCE],
            "startRunners": True,
        },
        ws_context,
    )

    assert result["schemas"][_ACTION_SOURCE] is not None
    assert recorded == [{tmp_path: [_ACTION_NAME]}]


async def test_no_start_runners_leaves_schema_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passive callers (MCP's startup tool listing) must not start handler
    environments. The default keeps the old behaviour: a schema that is not
    already available stays None.
    """
    _project, ws_context = _make_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )

    async def fail_start_required_environments(*args: object, **kwargs: object) -> None:
        raise AssertionError("start_required_environments must not be called")

    monkeypatch.setattr(
        run_service, "start_required_environments", fail_start_required_environments
    )

    result = await _handle_get_payload_schemas(
        {"project": str(tmp_path), "actionSources": [_ACTION_SOURCE]},
        ws_context,
    )

    assert result["schemas"][_ACTION_SOURCE] is None


async def test_start_runners_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the requested environments cannot be started, the caller must see
    the failure rather than a silent None. The run's own gate would raise the
    same error a moment later.
    """
    _project, ws_context = _make_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )

    async def raise_start_required_environments(
        *args: object, **kwargs: object
    ) -> None:
        raise run_service.StartingEnvironmentsFailed("x")

    monkeypatch.setattr(
        run_service, "start_required_environments", raise_start_required_environments
    )

    with pytest.raises(run_service.StartingEnvironmentsFailed):
        await _handle_get_payload_schemas(
            {
                "project": str(tmp_path),
                "actionSources": [_ACTION_SOURCE],
                "startRunners": True,
            },
            ws_context,
        )


def _make_matrix_ws_context(
    tmp_path: Path,
) -> tuple[domain.ResolvedProject, context.WorkspaceContext]:
    """Project with a matrixed action plus raw config declaring two children
    under a ``default_interpreters = {local = "newest"}`` policy."""
    project = make_multi_env_action_project(
        dir_path=tmp_path,
        action_name=_ACTION_NAME,
        handler_envs=["testing@cpython-3.13", "testing@cpython-3.14"],
    )
    for handler in project.actions[0].handlers:
        if handler.env == "testing@cpython-3.13":
            handler.interpreter = "cpython@3.13"
        elif handler.env == "testing@cpython-3.14":
            handler.interpreter = "cpython@3.14"
    ws_context = make_workspace_context(
        project=project,
        runner=make_initializing_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    ws_context.ws_projects_raw_configs[tmp_path] = {
        "tool": {
            "finecode": {
                "env": {
                    "testing@cpython-3.13": {
                        "interpreter": "cpython@3.13",
                        "default_interpreters": {"local": "newest"},
                    },
                    "testing@cpython-3.14": {
                        "interpreter": "cpython@3.14",
                        "default_interpreters": {"local": "newest"},
                    },
                }
            }
        }
    }
    return project, ws_context


async def test_schema_fetch_passes_local_selection_to_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``devEnv: cli`` the fetch starts only the child the run would
    select — the newest under the local default policy."""
    project, ws_context = _make_matrix_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )
    recorded: list[object] = []

    async def fake_start_required_environments(
        _actions_by_projects: dict[Path, list[str]],
        ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        recorded.append(kwargs.get("selected_interpreters_by_project"))
        ws_context.ws_projects_extension_runners[project.dir_path][
            "testing@cpython-3.14"
        ] = make_running_runner(
            working_dir_path=tmp_path, env_name="testing@cpython-3.14"
        )

    monkeypatch.setattr(
        run_service, "start_required_environments", fake_start_required_environments
    )

    result = await _handle_get_payload_schemas(
        {
            "project": str(tmp_path),
            "actionSources": [_ACTION_SOURCE],
            "startRunners": True,
            "runOptions": {"devEnv": "cli"},
        },
        ws_context,
    )

    assert result["schemas"][_ACTION_SOURCE] is not None
    assert recorded == [{tmp_path: {"cpython@3.14"}}]


async def test_schema_fetch_passes_no_narrowing_for_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``devEnv: ci`` the full axis runs, so the gate gets no narrowing."""
    _project, ws_context = _make_matrix_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )
    recorded: list[object] = []

    async def fake_start_required_environments(
        _actions_by_projects: dict[Path, list[str]],
        _ws_context: context.WorkspaceContext,
        **kwargs: object,
    ) -> None:
        recorded.append(kwargs.get("selected_interpreters_by_project"))

    monkeypatch.setattr(
        run_service, "start_required_environments", fake_start_required_environments
    )

    await _handle_get_payload_schemas(
        {
            "project": str(tmp_path),
            "actionSources": [_ACTION_SOURCE],
            "startRunners": True,
            "runOptions": {"devEnv": "ci"},
        },
        ws_context,
    )

    assert recorded == [{tmp_path: None}]


async def test_schema_fetch_with_unknown_selector_starts_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed selector unknown in this project resolves to an empty
    set, which the gate receives as-is: no matrix child is started. The run's
    own validation reports the selector error a moment later."""
    _project, ws_context = _make_matrix_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )
    gate_selections: list[object] = []
    real_gate = run_service.start_required_environments

    async def spy_gate(
        actions_by_projects: dict[Path, list[str]],
        gate_ws_context: context.WorkspaceContext,
        **kwargs: typing.Any,
    ) -> None:
        gate_selections.append(kwargs.get("selected_interpreters_by_project"))
        await real_gate(actions_by_projects, gate_ws_context, **kwargs)

    monkeypatch.setattr(run_service, "start_required_environments", spy_gate)
    started: list[str] = []

    async def _recording_start_runner(**kwargs: object) -> None:
        started.append(kwargs["env_name"])  # type: ignore[arg-type]

    monkeypatch.setattr(runner_manager, "start_runner", _recording_start_runner)

    result = await _handle_get_payload_schemas(
        {
            "project": str(tmp_path),
            "actionSources": [_ACTION_SOURCE],
            "startRunners": True,
            "runOptions": {"interpreterSelectors": ["x"]},
        },
        ws_context,
    )

    assert result["schemas"][_ACTION_SOURCE] is None
    assert gate_selections == [{tmp_path: set()}]
    assert all(not env.startswith("testing@") for env in started)


async def test_schema_fetch_with_malformed_selector_skips_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed selector raises inside the selection, so the fetch skips
    the gate without failing: the run's own validation reports the selector
    error a moment later."""
    _project, ws_context = _make_matrix_ws_context(tmp_path)
    monkeypatch.setattr(
        runner_client,
        "get_payload_schemas",
        _fake_get_payload_schemas({_ACTION_NAME: {"properties": {}}}),
    )

    async def fail_start_required_environments(*args: object, **kwargs: object) -> None:
        raise AssertionError("start_required_environments must not be called")

    monkeypatch.setattr(
        run_service, "start_required_environments", fail_start_required_environments
    )

    result = await _handle_get_payload_schemas(
        {
            "project": str(tmp_path),
            "actionSources": [_ACTION_SOURCE],
            "startRunners": True,
            "runOptions": {"interpreterSelectors": ["a@b@c"]},
        },
        ws_context,
    )

    assert result["schemas"][_ACTION_SOURCE] is None
