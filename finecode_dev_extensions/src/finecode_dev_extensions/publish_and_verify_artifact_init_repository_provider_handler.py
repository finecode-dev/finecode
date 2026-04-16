import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.publishing import init_repository_provider_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    Repository,
    RepositoryCredentials,
)

from .publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
    PublishAndVerifyArtifactRunContext,
    PublishAndVerifyArtifactRunPayload,
    PublishAndVerifyArtifactRunResult,
)


@dataclasses.dataclass
class PublishAndVerifyArtifactInitRepositoryProviderHandlerConfig(
    code_action.ActionHandlerConfig,
):
    repositories: list[Repository] = dataclasses.field(default_factory=list)
    credentials_by_repository: dict[str, RepositoryCredentials] = dataclasses.field(
        default_factory=dict
    )


class PublishAndVerifyArtifactInitRepositoryProviderHandler(
    code_action.ActionHandler[
        PublishAndVerifyArtifactAction,
        PublishAndVerifyArtifactInitRepositoryProviderHandlerConfig,
    ]
):
    def __init__(
        self,
        config: PublishAndVerifyArtifactInitRepositoryProviderHandlerConfig,
        action_runner: iprojectactionrunner.IProjectActionRunner,
    ) -> None:
        self.config = config
        self.action_runner = action_runner

    async def run(
        self,
        payload: PublishAndVerifyArtifactRunPayload,
        run_context: PublishAndVerifyArtifactRunContext,
    ) -> PublishAndVerifyArtifactRunResult:
        run_meta = run_context.meta

        await self.action_runner.run_action(
            action_type=init_repository_provider_action.InitRepositoryProviderAction,
            payload=init_repository_provider_action.InitRepositoryProviderRunPayload(
                repositories=self.config.repositories,
                credentials_by_repository=self.config.credentials_by_repository,
            ),
            meta=run_meta,
        )

        return PublishAndVerifyArtifactRunResult(
            version="",
            published_registries=[],
            verification_errors={},
        )
