"""Auto-repair must create a matrix child venv with that child's interpreter.

Without the interpreter in the env spec, discovery is skipped and uv runs
``venv`` without ``--python`` — a missing ``testing@cpython-3.11`` venv is
recreated with the default Python under the wrong name.
"""

from __future__ import annotations

import pathlib
from unittest import mock

from finecode.wm_server import context, domain
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.services import prepare_envs_service


def _make_context(
    tmp_path: pathlib.Path, raw_env_table: dict[str, dict]
) -> tuple[context.WorkspaceContext, domain.ResolvedProject]:
    project_dir = tmp_path / "p"
    project_dir.mkdir(exist_ok=True)
    project = wm_testing.make_single_action_project(
        dir_path=project_dir,
        action_name="test_action",
        handler_env="testing@cpython-3.13",
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[project.dir_path] = project
    ws_context.ws_projects_raw_configs[project.dir_path] = {
        "tool": {"finecode": {"env": raw_env_table}}
    }
    return ws_context, project


async def test_matrix_child_interpreter_is_forwarded(
    tmp_path: pathlib.Path,
) -> None:
    """The env spec for a matrix child carries that child's interpreter in
    both the create and the install calls, so the venv is built with the
    right Python."""
    ws_context, project = _make_context(
        tmp_path, {"testing@cpython-3.13": {"interpreter": "cpython@3.13"}}
    )
    seen: list[tuple[str, dict]] = []

    async def _recorder(action_source: str, params: dict, *args, **kwargs):
        seen.append((action_source, params))

    async def _noop_start(*args, **kwargs) -> None:
        pass

    with (
        mock.patch.object(
            prepare_envs_service, "_run_env_action", side_effect=_recorder
        ),
        mock.patch(
            "finecode.wm_server.services.runner_start_service.start_runners_with_auto_prepare",
            side_effect=_noop_start,
        ),
    ):
        await prepare_envs_service.install_env_for_project(
            project, "testing@cpython-3.13", ws_context
        )

    assert [source for source, _ in seen] == [
        "fine_envs.CreateEnvsAction",
        "fine_envs.InstallEnvsAction",
    ]
    for _, params in seen:
        assert params["envs"][0]["interpreter"] == "cpython@3.13"


async def test_plain_env_spec_has_no_interpreter_key(tmp_path: pathlib.Path) -> None:
    """An env with no ``interpreter`` entry sends a spec without the key —
    byte-identical to today's payload."""
    ws_context, project = _make_context(tmp_path, {"dev_no_runtime": {}})
    project_dev = wm_testing.make_single_action_project(
        dir_path=project.dir_path,
        action_name="test_action",
        handler_env="dev_no_runtime",
    )
    ws_context.ws_projects[project.dir_path] = project_dev
    seen: list[tuple[str, dict]] = []

    async def _recorder(action_source: str, params: dict, *args, **kwargs):
        seen.append((action_source, params))

    async def _noop_start(*args, **kwargs) -> None:
        pass

    with (
        mock.patch.object(
            prepare_envs_service, "_run_env_action", side_effect=_recorder
        ),
        mock.patch(
            "finecode.wm_server.services.runner_start_service.start_runners_with_auto_prepare",
            side_effect=_noop_start,
        ),
    ):
        await prepare_envs_service.install_env_for_project(
            project_dev, "dev_no_runtime", ws_context
        )

    assert len(seen) == 2
    for _, params in seen:
        assert "interpreter" not in params["envs"][0]
