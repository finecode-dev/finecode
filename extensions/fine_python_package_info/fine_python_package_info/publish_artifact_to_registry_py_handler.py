import asyncio
import dataclasses

from twine import settings as twine_settings
from twine.commands import upload as twine_upload

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    publish_artifact_to_registry as publish_artifact_to_registry_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class PublishArtifactToRegistryPyHandlerConfig(code_action.ActionHandlerConfig):
    ...


class PublishArtifactToRegistryPyHandler(
    code_action.ActionHandler[
        publish_artifact_to_registry_action.PublishArtifactToRegistryAction,
        PublishArtifactToRegistryPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: PublishArtifactToRegistryPyHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.command_runner = command_runner
        self.logger = logger

    async def run(
        self,
        payload: publish_artifact_to_registry_action.PublishArtifactToRegistryRunPayload,
        run_context: publish_artifact_to_registry_action.PublishArtifactToRegistryRunContext,
    ) -> publish_artifact_to_registry_action.PublishArtifactToRegistryRunResult:
        # Get project metadata
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=payload.src_artifact_def_path
        )

        # Get registry URL from config
        tool_config = src_artifact_raw_def.get("tool", {})
        finecode_config = tool_config.get("finecode", {})
        registries_raw = finecode_config.get("registries", [])

        registry_url = None
        for registry in registries_raw:
            if registry.get("name") == payload.registry_name:
                registry_url = registry.get("url")
                break

        if registry_url is None:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' not found in configuration"
            )

        # Configure twine settings
        upload_settings = twine_settings.Settings(
            repository_url=registry_url,
            skip_existing=not payload.force,
            non_interactive=True,
            verbose=False,
        )

        # Run twine upload in executor to avoid blocking
        dist_artifact_paths = payload.dist_artifact_paths
        self.logger.info(
            f"Publishing {dist_artifact_paths} to {payload.registry_name}..."
        )

        try:
            await asyncio.to_thread(twine_upload.upload, upload_settings, [dist_artifact_path.as_posix() for dist_artifact_path in dist_artifact_paths])
        except Exception as e:
            raise code_action.ActionFailedException(
                f"Failed to upload package: {str(e)}"
            ) from e

        self.logger.info(
            f"Successfully published {dist_artifact_paths} to {payload.registry_name}"
        )

        return publish_artifact_to_registry_action.PublishArtifactToRegistryRunResult(
        )
