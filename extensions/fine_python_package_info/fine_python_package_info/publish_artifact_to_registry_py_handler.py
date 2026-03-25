import asyncio
import dataclasses

import requests
from twine import settings as twine_settings
from twine.commands import upload as twine_upload

from finecode_extension_api import code_action
from finecode_extension_api.actions.publishing import publish_artifact_to_registry_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    ilogger,
    irepositorycredentialsprovider,
)


@dataclasses.dataclass
class PublishArtifactToRegistryPyHandlerConfig(code_action.ActionHandlerConfig):
    verbose: bool = False


class PublishArtifactToRegistryPyHandler(
    code_action.ActionHandler[
        publish_artifact_to_registry_action.PublishArtifactToRegistryAction,
        PublishArtifactToRegistryPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: PublishArtifactToRegistryPyHandlerConfig,
        command_runner: icommandrunner.ICommandRunner,
        logger: ilogger.ILogger,
        repository_credentials_provider: irepositorycredentialsprovider.IRepositoryCredentialsProvider,
    ) -> None:
        self.config = config
        self.command_runner = command_runner
        self.logger = logger
        self.repository_credentials_provider = repository_credentials_provider

    async def run(
        self,
        payload: publish_artifact_to_registry_action.PublishArtifactToRegistryRunPayload,
        run_context: publish_artifact_to_registry_action.PublishArtifactToRegistryRunContext,
    ) -> publish_artifact_to_registry_action.PublishArtifactToRegistryRunResult:
        # Get registry URL from repository provider
        repository = self.repository_credentials_provider.get_repository(payload.registry_name)
        if repository is None:
            raise code_action.ActionFailedException(
                f"Registry '{payload.registry_name}' not found in repository provider"
            )
        upload_url = f"{repository.url.rstrip('/')}/legacy/"

        # Get credentials from provider
        credentials = self.repository_credentials_provider.get_credentials(payload.registry_name)
        username = credentials.username if credentials else None
        password = credentials.password if credentials else None

        # Configure twine settings
        upload_settings = twine_settings.Settings(
            repository_url=upload_url,
            skip_existing=not payload.force,
            non_interactive=True,
            verbose=self.config.verbose,
            username=username,
            password=password
        )

        # Run twine upload in executor to avoid blocking
        dist_artifact_paths = payload.dist_artifact_paths
        self.logger.info(
            f"Publishing {dist_artifact_paths} to {payload.registry_name}..."
        )

        try:
            await asyncio.to_thread(twine_upload.upload, upload_settings, [dist_artifact_path.as_posix() for dist_artifact_path in dist_artifact_paths])
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            response_body = e.response.text if e.response is not None else None
            raise code_action.ActionFailedException(
                f"Failed to upload package: {str(e)}\nStatus code: {status_code}\nResponse body: {response_body}"
            ) from e
        except Exception as e:
            raise code_action.ActionFailedException(
                f"Failed to upload package: {str(e)}"
            ) from e

        self.logger.info(
            f"Successfully published {dist_artifact_paths} to {payload.registry_name}"
        )

        return publish_artifact_to_registry_action.PublishArtifactToRegistryRunResult(
        )
