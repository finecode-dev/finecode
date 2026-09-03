"""`er_dispatch` forwards per-project payload overrides with normalized keys.

The ER serializes override keys with ``Path.as_posix()`` while ``proxy_utils``
looks them up with ``str(Path)``. Identical on POSIX, but on Windows the two
forms differ (``C:\\ws\\proj`` vs ``C:/ws/proj``), so every lookup would miss
and each project would silently receive the empty base payload. The boundary
normalizes with ``str(Path(k))`` so the two sides always agree (ADR-0090 D-B1).
"""

from __future__ import annotations

import pathlib
from unittest import mock

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import _internal_client_types
from finecode.wm_server.services.run_service import er_dispatch

_CANONICAL_SOURCE = "test.actions.TestAction"


def _make_params(
    project_paths: list[str] | None,
) -> _internal_client_types.RunActionInWorkspaceParams:
    return _internal_client_types.RunActionInWorkspaceParams(
        action_source=_CANONICAL_SOURCE,
        payload={},
        meta=_internal_client_types.RunActionInProjectMeta(
            trigger="user", dev_env="cli", orchestration_depth=0
        ),
        project_paths=project_paths,
    )


def _make_context(dir_path: pathlib.Path):
    project = wm_testing.make_single_action_project(
        dir_path=dir_path,
        action_name="test_action",
        action_source=_CANONICAL_SOURCE,
    )
    project.actions[0].canonical_source = _CANONICAL_SOURCE
    runner = wm_testing.make_running_runner(
        working_dir_path=dir_path, env_name="test_env"
    )
    runner.client.configure_response(
        wm_testing.make_run_action_response(result_by_format={"json": {}})
    )
    return runner, wm_testing.make_workspace_context(project=project, runner=runner)


async def test_payload_overrides_are_forwarded_with_normalized_keys(
    tmp_path: pathlib.Path,
) -> None:
    runner, ws_context = _make_context(tmp_path)

    # The form the ER sends is `as_posix()`. On Windows this differs from
    # `str(Path)`; on POSIX it happens to be identical, so this test only
    # proves the Windows fix on the Windows CI leg — which is the point of
    # using a Windows-style key rather than a POSIX path.
    windows_key = "C:/ws/proj"
    params = _make_params([tmp_path.as_posix()])
    params.payload_overrides_by_project = {
        windows_key: {"file_paths": ["a.py"]},
    }

    captured: dict = {}

    async def _fake_run_actions_in_projects(self, **kwargs):
        captured.update(kwargs)
        return {}

    with mock.patch.object(
        er_dispatch.WorkspaceExecutor, "run_actions_in_projects", _fake_run_actions_in_projects
    ):
        await er_dispatch._BridgeHandlers().run_action_in_workspace(
            runner=runner, params=params, ws_context=ws_context
        )

    forwarded = captured["payload_overrides_by_project"]
    # The fan-out looks up by `str(Path)`, so that is the form that must be
    # present — never the raw `as_posix()` string when the two differ.
    assert forwarded == {
        str(pathlib.Path(windows_key)): {"file_paths": ["a.py"]},
    }
