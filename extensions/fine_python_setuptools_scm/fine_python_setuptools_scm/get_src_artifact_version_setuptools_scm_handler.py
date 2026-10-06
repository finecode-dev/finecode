import dataclasses

from fine_src_artifacts import get_src_artifact_version_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from setuptools_scm._get_version import _get_version

from ._scm_config import load_configuration, resolve_def_path


@dataclasses.dataclass
class GetSrcArtifactVersionSetuptoolsScmHandlerConfig(
    code_action.ActionHandlerConfig
): ...


class GetSrcArtifactVersionSetuptoolsScmHandler(
    code_action.ActionHandler[
        get_src_artifact_version_action.GetSrcArtifactVersionAction,
        GetSrcArtifactVersionSetuptoolsScmHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GetSrcArtifactVersionSetuptoolsScmHandlerConfig,
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
        # Use current project if src_artifact_def_path is not provided
        src_artifact_def_path = resolve_def_path(
            payload.src_artifact_def_path, self.project_info_provider
        )

        src_artifact_raw_def = await self.project_info_provider.get_project_raw_config(
            project_def_path=src_artifact_def_path
        )

        # Check that version is dynamic
        dynamic_fields = src_artifact_raw_def.get("project", {}).get("dynamic", [])
        if "version" not in dynamic_fields:
            raise code_action.ActionFailedException(
                f"Version is not dynamic in {src_artifact_def_path}, "
                "this handler only supports dynamic versions via setuptools_scm"
            )

        # from setuptools_scm._cli:main
        config = load_configuration(src_artifact_def_path, self.logger)

        version = _get_version(config, force_write_version_files=True)
        if version is None:
            raise code_action.ActionFailedException("ERROR: no version found")

        # from setuptools_scm._cli:main end

        return get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
            version=version
        )
