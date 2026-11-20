from __future__ import annotations

from pathlib import Path

import pytest

from finecode.wm_server import domain, testing
from finecode.wm_server._api_handlers import _helpers, _workspace
from finecode.wm_server._api_handlers._streaming import _resolve_source_to_name
from finecode.wm_server.config.interpreter_matrix import MATRIX_ENV_KEY
from finecode.wm_server.errors import ActionNotResolvableError
from finecode.wm_server.runner import runner_client, runner_manager
from finecode.wm_server.services import action_meta_cache, run_service
from finecode.wm_server.services.run_service import proxy_utils


def _matrix_project(path: Path):
    project = testing.make_multi_env_action_project(
        dir_path=path,
        action_name="run_tests",
        handler_envs=["testing@cpython-3.11", "testing@cpython-3.14"],
    )
    ws_context = testing.make_workspace_context(
        project=project,
        runner=testing.make_running_runner(
            working_dir_path=path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    envs = {}
    for handler, version in zip(project.actions[0].handlers, ["3.11", "3.14"]):
        handler.interpreter = f"cpython@{version}"
        envs[handler.env] = {
            MATRIX_ENV_KEY: "testing",
            "interpreter": handler.interpreter,
            "default_interpreters": {"local": "newest"},
        }
    ws_context.ws_projects_raw_configs[path] = {"tool": {"finecode": {"env": envs}}}
    return project, ws_context


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, "testing@cpython-3.14"),
        ({"interpreterSelectors": ["3.14"]}, "testing@cpython-3.14"),
        ({"interpreterSelectors": ["3.11"]}, "testing@cpython-3.11"),
        ({"devEnv": "ci"}, "testing@cpython-3.11"),
        ({"interpreterSelectors": ["all"]}, "testing@cpython-3.11"),
    ],
)
async def test_single_run_metadata_starts_a_selected_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict, expected: str
) -> None:
    project, ws_context = _matrix_project(tmp_path)
    action = project.actions[0]
    started = []

    async def start(*, env_name, **kwargs):
        started.append(env_name)
        if env_name != expected:
            raise AssertionError(f"unselected environment was started: {env_name}")
        action.canonical_source = action.source
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", start)

    parsed = await _helpers._parse_and_validate_run_action_params(
        {"project": str(tmp_path), "actionSource": action.source, "options": options},
        ws_context,
    )

    assert parsed.action.canonical_source == action.source
    assert started == [expected]


async def test_batch_metadata_uses_local_default_before_scope_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, ws_context = _matrix_project(tmp_path)
    action = project.actions[0]
    started = []

    async def start(*, env_name, **kwargs):
        started.append(env_name)
        assert env_name == "testing@cpython-3.14"
        action.canonical_source = action.source
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", start)

    actions, _ = await _helpers._resolve_actions_by_project(
        None, [action.source], ws_context
    )

    assert actions == {tmp_path: [action.name]}
    assert started == ["testing@cpython-3.14"]


@pytest.mark.parametrize("selectors", [["3.11"], ["3.14"]])
async def test_streamed_source_resolution_preserves_interpreter_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selectors: list[str]
) -> None:
    project, ws_context = _matrix_project(tmp_path)
    action = project.actions[0]
    started = []

    async def start(*, env_name, **kwargs):
        started.append(env_name)
        action.canonical_source = action.source
        action.scope = domain.ActionScope.PROJECT

    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", start)

    name = await _resolve_source_to_name(
        action.source,
        "",
        ws_context,
        run_options={"interpreterSelectors": selectors},
    )

    assert name == action.name
    assert started == [f"testing@cpython-{selectors[0]}"]


async def test_needed_metadata_does_not_fall_back_to_an_unselected_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, ws_context = _matrix_project(tmp_path)
    started = []

    async def fail_start(*, env_name, **kwargs):
        started.append(env_name)
        raise run_service.StartingEnvironmentsFailed("selected env is broken")

    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", fail_start)

    with pytest.raises(ActionNotResolvableError, match="testing@cpython-3.14"):
        await _helpers._parse_and_validate_run_action_params(
            {"project": str(tmp_path), "actionSource": project.actions[0].source},
            ws_context,
        )

    assert started == ["testing@cpython-3.14"]


async def test_warm_selected_runner_restores_metadata_after_project_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, ws_context = _matrix_project(tmp_path)
    action = project.actions[0]
    ws_context.ws_projects_extension_runners[tmp_path]["testing@cpython-3.14"] = (
        testing.make_running_runner(
            working_dir_path=tmp_path, env_name="testing@cpython-3.14"
        )
    )
    queried = []

    async def metadata(runner):
        queried.append(runner.env_name)
        return {
            "actions": {
                action.source: {
                    "canonical_source": action.source,
                    "scope": "project",
                    "runs_concurrently": False,
                }
            }
        }

    async def fail_start(**kwargs):
        raise AssertionError("the selected runner is already running")

    monkeypatch.setattr(runner_client, "resolve_action_meta", metadata)
    monkeypatch.setattr(runner_manager, "start_runner", fail_start)

    parsed = await _helpers._parse_and_validate_run_action_params(
        {"project": str(tmp_path), "actionSource": action.source}, ws_context
    )

    assert parsed.action.canonical_source == action.source
    assert parsed.action.scope == domain.ActionScope.PROJECT
    assert queried == ["testing@cpython-3.14"]


async def test_batch_can_read_scope_without_starting_a_deselected_root_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    project, ws_context = _matrix_project(root)
    other, other_context = _matrix_project(sibling)
    project.actions[0].handlers = project.actions[0].handlers[:1]
    ws_context.ws_projects_raw_configs[root]["tool"]["finecode"]["env"].pop(
        "testing@cpython-3.14"
    )
    ws_context.ws_projects[sibling] = other
    ws_context.ws_projects_raw_configs.update(other_context.ws_projects_raw_configs)
    looked_up = []

    async def lookup(project, ws_context, *, actions):
        looked_up.append(project.dir_path)
        for action in actions:
            action.canonical_source = action.source
            action.scope = domain.ActionScope.PROJECT
        return {}

    async def fail_start(**kwargs):
        raise AssertionError("no selected handler exists in the root")

    monkeypatch.setattr(action_meta_cache, "resolve_unresolved", lookup)
    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", fail_start)

    actions, _ = await _helpers._resolve_actions_by_project(
        None,
        [project.actions[0].source],
        ws_context,
        run_options={"interpreterSelectors": ["3.14"]},
    )

    assert actions == {root: ["run_tests"], sibling: ["run_tests"]}
    assert looked_up == [root]


@pytest.mark.parametrize("cached_scope", [None, domain.ActionScope.WORKSPACE])
@pytest.mark.parametrize("cache_raises", [False, True])
async def test_named_listing_never_starts_root_handler_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached_scope, cache_raises: bool
) -> None:
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    project, ws_context = _matrix_project(root)
    other = testing.make_single_action_project(
        dir_path=sibling, action_name="run_tests"
    )
    ws_context.ws_projects[sibling] = other
    looked_up = []

    async def lookup(project, ws_context, *, actions):
        looked_up.append(project.dir_path)
        if cache_raises:
            raise RuntimeError("cache dump failed")
        if cached_scope is not None:
            for action in actions:
                action.canonical_source = action.source
                action.scope = cached_scope
        return {}

    async def fail_start(**kwargs):
        raise AssertionError("listing must not start a handler runner")

    monkeypatch.setattr(run_service, "resolve_unresolved_metadata", lookup)
    monkeypatch.setattr(proxy_utils, "_start_runner_or_update_config", fail_start)

    result = await _workspace._handle_list_actions({"names": ["run_tests"]}, ws_context)

    paths = {item["project"] for item in result["actions"]}
    assert root in looked_up
    if cached_scope is None or cache_raises:
        assert paths == {str(root), str(sibling)}
        assert all(item["scope"] is None for item in result["actions"])
    else:
        assert paths == {str(root)}
        assert result["actions"][0]["scope"] == "workspace"
