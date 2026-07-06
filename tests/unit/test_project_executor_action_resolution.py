from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.services.run_service import ProjectExecutor, exceptions, proxy_utils


def _build_session(tmp_path: pathlib.Path):
    client = wm_testing.FakeErClient()
    runner = wm_testing.make_running_runner(working_dir_path=tmp_path, client=client)
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path, action_name="test_action"
    )
    ws_context = wm_testing.make_workspace_context(project=project, runner=runner)
    return client, project, ws_context


async def test_run_action_retries_metadata_resolution_when_canonical_source_unresolved(
    tmp_path: pathlib.Path,
) -> None:
    """An action may still be unresolved (``canonical_source is None``) when a
    run is requested for it, because no prior request has started the env
    that hosts its handlers. ``run_action`` must attempt to resolve it via
    ``ensure_action_metadata`` before concluding the action doesn't exist.
    """
    client, project, ws_context = _build_session(tmp_path)
    action_source = project.actions[0].source
    client.configure_response(
        wm_testing.make_run_action_response(
            return_code=0, result_by_format={"json": {}}
        )
    )

    async def _fake_ensure_action_metadata(action, project_arg, ws_context_arg):
        action.canonical_source = action_source  # simulate the env resolving it

    with mock.patch.object(
        proxy_utils, "ensure_action_metadata", side_effect=_fake_ensure_action_metadata
    ):
        result = await ProjectExecutor(ws_context).run_action(
            action_source=action_source,
            params={},
            project_path=project.dir_path,
            run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
            dev_env=proxy_utils.DevEnv.CI,
        )

    assert result.return_code == 0


async def test_run_action_still_fails_when_metadata_cannot_resolve(
    tmp_path: pathlib.Path,
) -> None:
    """When metadata resolution cannot recover a matching action (the source
    genuinely doesn't exist in this project), the original "no such action"
    failure must still surface, unmasked.
    """
    _client, project, ws_context = _build_session(tmp_path)

    async def _noop_ensure_action_metadata(action, project_arg, ws_context_arg):
        return None  # canonical_source stays unresolved

    with mock.patch.object(
        proxy_utils, "ensure_action_metadata", side_effect=_noop_ensure_action_metadata
    ):
        with pytest.raises(exceptions.ActionRunFailed):
            await ProjectExecutor(ws_context).run_action(
                action_source="does.not.Exist",
                params={},
                project_path=project.dir_path,
                run_trigger=proxy_utils.RunActionTrigger.SYSTEM,
                dev_env=proxy_utils.DevEnv.CI,
            )
