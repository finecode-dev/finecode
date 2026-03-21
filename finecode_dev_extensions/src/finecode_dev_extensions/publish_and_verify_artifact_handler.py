import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.actions.publishing import (
    get_dist_artifact_version_action,
    publish_artifact_action,
    verify_artifact_published_to_registry_action,
)
from finecode_extension_api.interfaces import iactionrunner, iprojectinfoprovider

from .publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
    PublishAndVerifyArtifactRunContext,
    PublishAndVerifyArtifactRunPayload,
    PublishAndVerifyArtifactRunResult,
)


@dataclasses.dataclass
class PublishAndVerifyArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class PublishAndVerifyArtifactHandler(
    code_action.ActionHandler[
        PublishAndVerifyArtifactAction,
        PublishAndVerifyArtifactHandlerConfig,
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
        payload: PublishAndVerifyArtifactRunPayload,
        run_context: PublishAndVerifyArtifactRunContext,
    ) -> PublishAndVerifyArtifactRunResult:
        run_meta = run_context.meta

        src_artifact_def_path: pathlib.Path = payload.src_artifact_def_path
        dist_artifact_paths: list[pathlib.Path] = payload.dist_artifact_paths

        # Publish the artifact
        publish_action = self.action_runner.get_action_by_name(
            "publish_artifact", publish_artifact_action.PublishArtifactAction
        )
        publish_payload = publish_artifact_action.PublishArtifactRunPayload(
            src_artifact_def_path=src_artifact_def_path,
            dist_artifact_paths=dist_artifact_paths,
            force=payload.force,
        )
        publish_result = await self.action_runner.run_action(
            action=publish_action, payload=publish_payload, meta=run_meta
        )
        published_registries = publish_result.published_registries

        # TODO: impl verify of each dist file. NOTE; they can have different versions
        # Get version from the dist artifact
        get_version_action = self.action_runner.get_action_by_name(
            "get_dist_artifact_version",
            get_dist_artifact_version_action.GetDistArtifactVersionAction,
        )
        get_version_payload = get_dist_artifact_version_action.GetDistArtifactVersionRunPayload(
            dist_artifact_path=dist_artifact_paths[0]
        )
        get_version_result = await self.action_runner.run_action(
            action=get_version_action, payload=get_version_payload, meta=run_meta
        )
        version = get_version_result.version

        
        # Verify each published registry
        verification_errors: dict[str, list[str]] = {}
        verify_action = self.action_runner.get_action_by_name(
            "verify_artifact_published_to_registry",
            verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryAction,
        )

        for registry_name in published_registries:
            verify_payload = verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunPayload(
                dist_artifact_paths=dist_artifact_paths,
                registry_name=registry_name,
                version=version,
            )
            verify_result = await self.action_runner.run_action(
                action=verify_action, payload=verify_payload, meta=run_meta
            )
            if verify_result.errors:
                verification_errors[registry_name] = verify_result.errors

        return PublishAndVerifyArtifactRunResult(
            version=version,
            published_registries=published_registries,
            verification_errors=verification_errors,
        )
