import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    get_src_artifact_registries as get_src_artifact_registries_action
from finecode_extension_api.interfaces import (
    ilogger,
    irepositorycredentialsprovider,
)


@dataclasses.dataclass
class GetSrcArtifactRegistriesPyHandlerConfig(code_action.ActionHandlerConfig): ...


class GetSrcArtifactRegistriesPyHandler(
    code_action.ActionHandler[
        get_src_artifact_registries_action.GetSrcArtifactRegistriesAction,
        GetSrcArtifactRegistriesPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GetSrcArtifactRegistriesPyHandlerConfig,
        repository_credentials_provider: irepositorycredentialsprovider.IRepositoryCredentialsProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.repository_credentials_provider = repository_credentials_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_registries_action.GetSrcArtifactRegistriesRunPayload,
        run_context: get_src_artifact_registries_action.GetSrcArtifactRegistriesRunContext,
    ) -> get_src_artifact_registries_action.GetSrcArtifactRegistriesRunResult:
        repositories = self.repository_credentials_provider.get_all_repositories()

        registries = [
            get_src_artifact_registries_action.Registry(url=repo.url, name=repo.name)
            for repo in repositories
        ]

        return get_src_artifact_registries_action.GetSrcArtifactRegistriesRunResult(
            registries=registries
        )
