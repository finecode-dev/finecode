from __future__ import annotations

import pathlib
import sys
import typing
import uuid

import pytest
from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client, runner_manager
from finecode.wm_server.services import shutdown_service


@pytest.fixture
async def version_runner(
    tmp_path: pathlib.Path,
) -> typing.AsyncIterator[
    tuple[context.WorkspaceContext, runner_client.ExtensionRunnerInfo]
]:
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    _ = (caller_dir / "pyproject.toml").write_text(
        '[project]\nname = "caller"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    project = wm_testing.make_single_action_project(
        dir_path=caller_dir,
        action_name="get_src_artifact_version",
        handler_env="version_test",
        action_source="fine_src_artifacts.GetSrcArtifactVersionAction",
        handler_source="fine_python_package_info.GetSrcArtifactVersionPyHandler",
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[caller_dir] = project
    ws_context.ws_projects_raw_configs[caller_dir] = {
        "project": {"name": "caller", "version": "1.0.0"}
    }
    try:
        runner = await runner_manager.start_runner(
            project_def=project,
            env_name="version_test",
            handlers_to_initialize=None,
            ws_context=ws_context,
            cmd_override=sys.executable,
        )
        yield ws_context, runner
    finally:
        await shutdown_service.on_shutdown(ws_context)


async def _get_version(
    runner: runner_client.ExtensionRunnerInfo, payload: dict[str, typing.Any]
) -> str:
    result = await runner_client.run_action(
        runner,
        "get_src_artifact_version",
        payload,
        options={
            "resultFormats": ["json"],
            "runId": str(uuid.uuid4()),
            "meta": {"trigger": "user", "devEnv": "cli"},
        },
    )
    assert result.return_code == 0
    json_result = result.result_by_format["json"]
    assert isinstance(json_result, dict)
    return json_result["version"]


@pytest.mark.parametrize(
    "dirname", ["package", "space package", "café package", "percent%20package"]
)
async def test_explicit_uri_returns_artifact_resolved_version(
    version_runner: tuple[context.WorkspaceContext, runner_client.ExtensionRunnerInfo],
    tmp_path: pathlib.Path,
    dirname: str,
) -> None:
    """An explicit artifact URI selects that artifact's resolved version.

    The on-disk and current-project versions differ so falling back to either
    one would be detected.
    """
    ws_context, runner = version_runner
    artifact_dir = tmp_path / dirname
    artifact_dir.mkdir()
    definition = artifact_dir / "pyproject.toml"
    _ = definition.write_text(
        '[project]\nname = "artifact"\nversion = "0.0.1"\n', encoding="utf-8"
    )
    ws_context.ws_projects_raw_configs[artifact_dir] = {
        "project": {"name": "artifact", "version": "2.3.4"}
    }

    for uri in (
        definition.as_uri(),
        definition.as_uri().replace("package", "%70ackage"),
    ):
        assert await _get_version(runner, {"src_artifact_def_path": uri}) == "2.3.4"


async def test_default_returns_current_project_resolved_version(
    version_runner: tuple[context.WorkspaceContext, runner_client.ExtensionRunnerInfo],
) -> None:
    """Without an explicit definition the current project's version is used."""
    _, runner = version_runner

    assert await _get_version(runner, {}) == "1.0.0"


async def test_unknown_artifact_reports_config_lookup_error(
    version_runner: tuple[context.WorkspaceContext, runner_client.ExtensionRunnerInfo],
    tmp_path: pathlib.Path,
) -> None:
    """Unknown artifacts fail visibly instead of using the current project."""
    _, runner = version_runner
    unknown = tmp_path / "unknown" / "pyproject.toml"
    unknown.parent.mkdir()
    _ = unknown.write_text(
        '[project]\nname = "unknown"\nversion = "0.0.1"\n', encoding="utf-8"
    )

    with pytest.raises(runner_client.ActionRunFailed) as caught:
        await _get_version(runner, {"src_artifact_def_path": unknown.as_uri()})

    assert "Config of project" in str(caught.value)
    assert "not found" in str(caught.value)
    assert "unknown" in str(caught.value)
