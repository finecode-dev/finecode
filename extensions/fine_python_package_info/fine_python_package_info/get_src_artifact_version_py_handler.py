import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    get_src_artifact_version as get_src_artifact_version_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider


@dataclasses.dataclass
class GetSrcArtifactVersionPyHandlerConfig(code_action.ActionHandlerConfig): ...


class GetSrcArtifactVersionPyHandler(
    code_action.ActionHandler[
        get_src_artifact_version_action.GetSrcArtifactVersionAction,
        GetSrcArtifactVersionPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GetSrcArtifactVersionPyHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_version_action.GetSrcArtifactVersionRunPayload,
        run_context: get_src_artifact_version_action.GetSrcArtifactVersionRunContext,
    ) -> get_src_artifact_version_action.GetSrcArtifactVersionRunResult:
        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=payload.src_artifact_def_path
        )
        version = src_artifact_raw_def.get("project", {}).get("version", None)

        if version is None:
            dynamic_fields = src_artifact_raw_def.get('project', {}).get('dynamic', [])
            if 'version' in dynamic_fields:
                raise code_action.ActionFailedException(
                    f"Version is dynamic in {payload.src_artifact_def_path}, use the right handler for that"
                )
            else:
                raise code_action.ActionFailedException(
                    f"Version not found in {payload.src_artifact_def_path}"
                )

        if not isinstance(version, str):
            raise code_action.ActionFailedException(
                f"project.version in {payload.src_artifact_def_path} expected to be a string, but is {type(version)}"
            )

        return get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
            version=version
        )
