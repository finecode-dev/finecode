# docs: docs/reference/actions.md
"""
DYNAMIC RUNTIME SEEDING ONLY.

Static provisioning of ``IRepositoryCredentialsProvider`` is service config,
declared once via ``[[tool.finecode.service]]`` and resolved at ER bootstrap
(ADR-0068) -- it needs no init step. This handler exists only for the case
where credentials must be pushed in at run time (tokens fetched mid-session,
rotated on the fly). It injects the *concrete*
``ConfigRepositoryCredentialsProvider`` rather than the interface, because the
push-seed methods (``add_repository``/``set_credentials``) are no longer part
of the universal read-only interface -- they are implementation-specific. The
provider is registered (see
``finecode_extension_runner.di.bootstrap``), so the instance seeded here is the
same instance the read-only consumers (``publish_artifact_to_registry_py`` and
friends) resolve through the interface.
"""

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_runner.impls import repository_credentials_provider

from fine_dist_artifacts import init_repository_provider_action


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
        repository_credentials_provider: repository_credentials_provider.ConfigRepositoryCredentialsProvider,
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
                name=repository.name,
                index_url=repository.index_url,
                upload_url=repository.upload_url,
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
