from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import context, errors
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types, runner_manager
from finecode.wm_server.services.run_service import er_dispatch, proxy_utils

_CANONICAL = "test.actions.TestAction"


def _make_unresolved(tmp_path: pathlib.Path, source: str = "test.actions.TestAction"):
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action", action_source=source
    )
    project.actions[0].canonical_source = None
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path)
    runner.client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[tmp_path] = project
    ws_context.ws_projects_extension_runners[tmp_path] = {"test_env": runner}
    return runner, project, ws_context


def _params(source: str):
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=source,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=1
        ),
        project_paths=None,
    )


async def test_cache_resolved_canonical_dispatches(tmp_path: pathlib.Path) -> None:
    """A canonical the cache resolves dispatches without starting any runner."""
    runner, project, ws_context = _make_unresolved(tmp_path)

    async def _resolving_cache(project_arg, ws_context_arg, **kwargs):
        project.actions[0].canonical_source = _CANONICAL
        return {}

    with (
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_resolving_cache,
        ),
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        mock.patch.object(
            proxy_utils,
            "ensure_action_metadata",
            side_effect=AssertionError("must not start"),
        ),
    ):
        result = await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner, params=_params(_CANONICAL), ws_context=ws_context
        )

    assert result.results_by_project


async def test_unresolvable_canonical_raises_not_found(tmp_path: pathlib.Path) -> None:
    """A canonical nothing resolves raises, so a typo fails loudly at the boundary."""
    runner, project, ws_context = _make_unresolved(
        tmp_path, source="test.actions.OtherAction"
    )

    async def _empty_cache(*args, **kwargs):
        return {}

    with (
        mock.patch.object(
            proxy_utils.action_meta_cache,
            "resolve_unresolved",
            side_effect=_empty_cache,
        ),
        mock.patch.object(
            runner_manager, "start_runner", side_effect=AssertionError("must not start")
        ),
        pytest.raises(errors.ActionNotFoundError),
    ):
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner, params=_params(_CANONICAL), ws_context=ws_context
        )
