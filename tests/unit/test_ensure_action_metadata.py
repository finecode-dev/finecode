from __future__ import annotations

import pathlib
from unittest import mock

import pytest

from finecode.wm_server import testing as wm_testing
from finecode.wm_server.errors import ActionNotResolvableError
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services.run_service import proxy_utils

pytestmark = pytest.mark.anyio


def _build_project_and_context(tmp_path: pathlib.Path, *, handler_env: str = "dev_no_runtime"):
    project = wm_testing.make_single_action_project(
        dir_path=tmp_path,
        action_name="get_src_artifact_language",
        handler_env=handler_env,
    )
    ws_context = wm_testing.make_workspace_context(
        project=project,
        runner=wm_testing.make_running_runner(
            working_dir_path=tmp_path, env_name="dev_workspace"
        ),
        env_name="dev_workspace",
    )
    return project, ws_context


async def test_ensure_action_metadata_starts_the_handlers_env_to_resolve_canonical_source(
    tmp_path: pathlib.Path,
) -> None:
    """An unresolved action's ``canonical_source`` is only known to the ER that
    hosts its handlers, so ``ensure_action_metadata`` must start that specific
    env (not any already-running env in the project) and pick up whatever
    ``canonical_source`` that startup resolves.
    """
    project, ws_context = _build_project_and_context(tmp_path)
    action = project.actions[0]
    assert action.canonical_source is None

    async def _fake_start_runner(*, project_def, env_name, handlers_to_initialize, ws_context, **_):
        assert env_name == "dev_no_runtime"
        action.canonical_source = f"resolved.{action.source}"
        return wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )

    with mock.patch.object(runner_manager, "start_runner", side_effect=_fake_start_runner):
        await proxy_utils.ensure_action_metadata(action, project, ws_context)

    assert action.canonical_source == f"resolved.{action.source}"


async def test_ensure_action_metadata_raises_when_env_fails_to_start(
    tmp_path: pathlib.Path,
) -> None:
    """If the env that would resolve the action fails to start at all, the
    caller must see a clear ``ActionNotResolvableError`` instead of an opaque
    low-level exception or a silent no-op.
    """
    project, ws_context = _build_project_and_context(tmp_path)
    action = project.actions[0]

    async def _failing_start_runner(**_):
        raise runner_manager.RunnerFailedToStart("boom")

    with mock.patch.object(runner_manager, "start_runner", side_effect=_failing_start_runner):
        with pytest.raises(ActionNotResolvableError):
            await proxy_utils.ensure_action_metadata(action, project, ws_context)


async def test_ensure_action_metadata_raises_when_env_starts_but_class_stays_unresolved(
    tmp_path: pathlib.Path,
) -> None:
    """The resolution env may start successfully without ever importing the
    action class (e.g. it's not actually installed there) — canonical_source
    stays None, and that must still surface as ActionNotResolvableError
    rather than a silent success.
    """
    project, ws_context = _build_project_and_context(tmp_path)
    action = project.actions[0]

    async def _fake_start_runner(*, project_def, env_name, **_):
        return wm_testing.make_running_runner(
            working_dir_path=project_def.dir_path, env_name=env_name
        )

    with mock.patch.object(runner_manager, "start_runner", side_effect=_fake_start_runner):
        with pytest.raises(ActionNotResolvableError):
            await proxy_utils.ensure_action_metadata(action, project, ws_context)


async def test_ensure_action_metadata_raises_when_action_has_no_handlers(
    tmp_path: pathlib.Path,
) -> None:
    """An action with no registered handlers can never be resolved by any ER —
    fail fast instead of trying to start a nonexistent env."""
    project, ws_context = _build_project_and_context(tmp_path)
    action = project.actions[0]
    action.handlers = []

    with pytest.raises(ActionNotResolvableError):
        await proxy_utils.ensure_action_metadata(action, project, ws_context)
