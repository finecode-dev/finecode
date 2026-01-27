import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    get_src_artifact_registries as get_src_artifact_registries_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider


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
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_registries_action.GetSrcArtifactRegistriesRunPayload,
        run_context: get_src_artifact_registries_action.GetSrcArtifactRegistriesRunContext,
    ) -> get_src_artifact_registries_action.GetSrcArtifactRegistriesRunResult:
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=payload.src_artifact_def_path
        )

        # Registries are in tool.finecode.registries
        tool_config = src_artifact_raw_def.get("tool", {})
        finecode_config = tool_config.get("finecode", {})
        registries_raw = finecode_config.get("registries", [])

        if not isinstance(registries_raw, list):
            raise code_action.ActionFailedException(
                f"tool.finecode.registries in {payload.src_artifact_def_path} expected to be a list, but is {type(registries_raw)}"
            )

        registries = []
        for idx, registry_dict in enumerate(registries_raw):
            if not isinstance(registry_dict, dict):
                raise code_action.ActionFailedException(
                    f"Registry at index {idx} in {payload.src_artifact_def_path} expected to be a dict, but is {type(registry_dict)}"
                )

            url = registry_dict.get("url")
            name = registry_dict.get("name")

            if url is None:
                raise code_action.ActionFailedException(
                    f"Registry at index {idx} in {payload.src_artifact_def_path} is missing 'url' field"
                )

            if name is None:
                raise code_action.ActionFailedException(
                    f"Registry at index {idx} in {payload.src_artifact_def_path} is missing 'name' field"
                )

            if not isinstance(url, str):
                raise code_action.ActionFailedException(
                    f"Registry url at index {idx} in {payload.src_artifact_def_path} expected to be a string, but is {type(url)}"
                )

            if not isinstance(name, str):
                raise code_action.ActionFailedException(
                    f"Registry name at index {idx} in {payload.src_artifact_def_path} expected to be a string, but is {type(name)}"
                )

            registries.append(
                get_src_artifact_registries_action.Registry(url=url, name=name)
            )

        return get_src_artifact_registries_action.GetSrcArtifactRegistriesRunResult(
            registries=registries
        )
