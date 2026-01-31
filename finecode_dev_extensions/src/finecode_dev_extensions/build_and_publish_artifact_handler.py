import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions import build_artifact_action, publish_artifact
from finecode_extension_api.interfaces import iactionrunner, iprojectinfoprovider

from .build_and_publish_artifact_action import (
    BuildAndPublishArtifactAction,
    BuildAndPublishArtifactRunContext,
    BuildAndPublishArtifactRunPayload,
    BuildAndPublishArtifactRunResult,
)


@dataclasses.dataclass
class BuildAndPublishArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class BuildAndPublishArtifactHandler(
    code_action.ActionHandler[
        BuildAndPublishArtifactAction,
        BuildAndPublishArtifactHandlerConfig,
    ]
):
    action_runner: iactionrunner.IActionRunner
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: BuildAndPublishArtifactRunPayload,
        run_context: BuildAndPublishArtifactRunContext,
    ) -> BuildAndPublishArtifactRunResult:
        run_meta = run_context.meta

        # Resolve src_artifact_def_path
        src_artifact_def_path: pathlib.Path
        if payload.src_artifact_def_path is None:
            src_artifact_def_path = (
                self.project_info_provider.get_current_project_def_path()
            )
        else:
            src_artifact_def_path = payload.src_artifact_def_path

        # Build the artifact
        build_action = self.action_runner.get_action_by_name("build_artifact", build_artifact_action.BuildArtifactAction)
        build_payload = build_artifact_action.BuildArtifactRunPayload(
            src_artifact_def_path=src_artifact_def_path
        )
        build_result = await self.action_runner.run_action(
            action=build_action, payload=build_payload, meta=run_meta
        )

        dist_artifact_paths = build_result.build_output_paths
 
        # Publish the artifact
        publish_action = self.action_runner.get_action_by_name("publish_artifact", publish_artifact.PublishArtifactAction)
        publish_payload = publish_artifact.PublishArtifactRunPayload(
            src_artifact_def_path=src_artifact_def_path,
            dist_artifact_paths=dist_artifact_paths,
            force=payload.force,
        )
        publish_result = await self.action_runner.run_action(
            action=publish_action, payload=publish_payload, meta=run_meta
        )

        return BuildAndPublishArtifactRunResult(
            version=publish_result.version,
            published_registries=publish_result.published_registries,
        )
