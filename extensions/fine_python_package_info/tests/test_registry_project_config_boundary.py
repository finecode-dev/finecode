from __future__ import annotations

import pathlib
import sys
import typing
import uuid

import pytest
from finecode.wm_server import context
from finecode.wm_server import testing as wm_testing
from finecode.wm_server.runner import runner_client, runner_manager
from finecode.wm_server.runner._internal_client_types import (
    GetProjectRawConfigParams,
    GetProjectRawConfigResult,
)
from finecode.wm_server.services import shutdown_service
from finecode_extension_runner.impls.project_info_provider import ProjectInfoProvider

from fine_python_package_info.testing.local_registry import LocalRegistry


@pytest.fixture
async def registry_runner(
    tmp_path: pathlib.Path,
) -> typing.AsyncIterator[
    tuple[context.WorkspaceContext, runner_client.ExtensionRunnerInfo, LocalRegistry]
]:
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    _ = (caller_dir / "pyproject.toml").write_text(
        '[project]\nname = "caller-on-disk"\n', encoding="utf-8"
    )
    project = wm_testing.make_single_action_project(
        dir_path=caller_dir,
        action_name="list_published_artifacts",
        handler_env="registry_test",
        action_source="fine_dist_artifacts.ListPublishedArtifactsAction",
        handler_source="fine_python_package_info.ListPublishedArtifactsPythonHandler",
    )
    ws_context = context.WorkspaceContext(ws_dirs_paths=[tmp_path])
    ws_context.ws_projects[caller_dir] = project
    ws_context.ws_projects_raw_configs[caller_dir] = {
        "project": {"name": "caller-resolved"}
    }
    with LocalRegistry(tmp_path / "registry") as registry:
        ws_context.service_config_overrides = {
            "repository_credentials_provider": {
                "repositories": [
                    {
                        "name": "local",
                        "index_url": registry.index_url,
                        "upload_url": registry.upload_url,
                    }
                ]
            }
        }
        try:
            runner = await runner_manager.start_runner(
                project_def=project,
                env_name="registry_test",
                handlers_to_initialize=None,
                ws_context=ws_context,
                cmd_override=sys.executable,
            )
            yield ws_context, runner, registry
        finally:
            await shutdown_service.on_shutdown(ws_context)


@pytest.mark.parametrize(
    "dirname", ["package", "space package", "café package", "percent%20package"]
)
async def test_registry_check_uses_artifact_resolved_config(
    registry_runner: tuple[
        context.WorkspaceContext, runner_client.ExtensionRunnerInfo, LocalRegistry
    ],
    tmp_path: pathlib.Path,
    dirname: str,
) -> None:
    """Release checks find the artifact's resolved config even for encoded names.

    Using the caller's config or reading the definition directly would check
    the wrong package in the registry.
    """
    ws_context, runner, registry = registry_runner
    artifact_dir = tmp_path / dirname
    artifact_dir.mkdir()
    definition = artifact_dir / "pyproject.toml"
    _ = definition.write_text('[project]\nname = "disk-name"\n', encoding="utf-8")
    ws_context.ws_projects_raw_configs[artifact_dir] = {
        "project": {"name": "resolved_artifact"}
    }

    for uri in (
        definition.as_uri(),
        definition.as_uri().replace("package", "%70ackage"),
    ):
        result = await runner_client.run_action(
            runner,
            "list_published_artifacts",
            {
                "src_artifact_def_path": uri,
                "version": "0.1.0",
                "registry_name": "local",
            },
            options={
                "resultFormats": ["json"],
                "runId": str(uuid.uuid4()),
                "meta": {"trigger": "user", "devEnv": "cli"},
            },
            child_stops=None,
        )
        assert result.return_code == 0
        json_result = result.result_by_format["json"]
        assert isinstance(json_result, dict)
        assert json_result["filenames"] == []

    assert [(request.method, request.path) for request in registry.request_log] == [
        ("GET", "/simple/resolved-artifact/"),
        ("GET", "/simple/resolved-artifact/"),
    ]


async def test_registry_check_reports_unknown_artifact_config(
    registry_runner: tuple[
        context.WorkspaceContext, runner_client.ExtensionRunnerInfo, LocalRegistry
    ],
    tmp_path: pathlib.Path,
) -> None:
    """Unknown artifact projects fail visibly before any registry request."""
    _, runner, registry = registry_runner
    unknown = tmp_path / "unknown" / "pyproject.toml"
    unknown.parent.mkdir()
    _ = unknown.write_text('[project]\nname = "unknown"\n', encoding="utf-8")

    with pytest.raises(runner_client.ActionRunFailed) as caught:
        await runner_client.run_action(
            runner,
            "list_published_artifacts",
            {
                "src_artifact_def_path": unknown.as_uri(),
                "version": "0.1.0",
                "registry_name": "local",
            },
            options={
                "resultFormats": ["json"],
                "runId": str(uuid.uuid4()),
                "meta": {"trigger": "user", "devEnv": "cli"},
            },
            child_stops=None,
        )

    assert "Config of project" in str(caught.value)
    assert "not found" in str(caught.value)
    assert "unknown" in str(caught.value)
    assert registry.request_log == []


async def test_project_config_provider_accepts_absolute_filesystem_path(
    registry_runner: tuple[
        context.WorkspaceContext, runner_client.ExtensionRunnerInfo, LocalRegistry
    ],
    tmp_path: pathlib.Path,
) -> None:
    """Existing filesystem-path consumers still receive resolved project config."""
    ws_context, runner, _ = registry_runner
    definition = tmp_path / "absolute café project" / "pyproject.toml"
    definition.parent.mkdir()
    _ = definition.write_text('[project]\nname = "disk-name"\n', encoding="utf-8")
    resolved = {"project": {"name": "resolved-name"}}
    ws_context.ws_projects_raw_configs[definition.parent] = resolved
    assert runner.client is not None
    lookup = runner.client.feature_impls["projects/getRawConfig"]

    async def get_config(project_def_path: str) -> dict[str, typing.Any]:
        response: GetProjectRawConfigResult = await lookup(
            GetProjectRawConfigParams(project_def_path=project_def_path)
        )
        return response.config

    provider = ProjectInfoProvider(
        project_def_path_getter=lambda: definition,
        project_raw_config_getter=get_config,
        current_project_raw_config_version_getter=lambda: 0,
    )

    assert await provider.get_project_raw_config(definition) == resolved
