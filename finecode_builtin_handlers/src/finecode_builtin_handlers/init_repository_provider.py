import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    init_repository_provider as init_repository_provider_action,
)
from finecode_extension_api.interfaces import irepositorycredentialsprovider


@dataclasses.dataclass
class InitRepositoryProviderHandlerConfig(code_action.ActionHandlerConfig):
    pass


class InitRepositoryProviderHandler(
    code_action.ActionHandler[
        init_repository_provider_action.InitRepositoryProviderAction,
        InitRepositoryProviderHandlerConfig,
    ]
):
    def __init__(
        self,
        repository_credentials_provider: irepositorycredentialsprovider.IRepositoryCredentialsProvider,
    ) -> None:
        self.repository_credentials_provider = repository_credentials_provider

    async def run(
        self,
        payload: init_repository_provider_action.InitRepositoryProviderRunPayload,
        run_context: init_repository_provider_action.InitRepositoryProviderRunContext,
    ) -> init_repository_provider_action.InitRepositoryProviderRunResult:
        initialized_repositories: list[str] = []

        # Add repositories
        for repository in payload.repositories:
            self.repository_credentials_provider.add_repository(
                name=repository.name, url=repository.url
            )
            initialized_repositories.append(repository.name)

        # Set credentials
        for repo_name, credentials in payload.credentials_by_repository.items():
            self.repository_credentials_provider.set_credentials(
                repository_name=repo_name,
                username=credentials.username,
                password=credentials.password,
            )

        return init_repository_provider_action.InitRepositoryProviderRunResult(
            initialized_repositories=initialized_repositories
        )
