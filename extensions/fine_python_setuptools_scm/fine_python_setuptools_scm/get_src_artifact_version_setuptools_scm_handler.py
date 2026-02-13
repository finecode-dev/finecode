import dataclasses

from setuptools_scm import Configuration
from setuptools_scm._get_version_impl import _get_version

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    get_src_artifact_version as get_src_artifact_version_action,
)
from finecode_extension_api.interfaces import iprojectinfoprovider, ilogger


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
        logger: ilogger.ILogger
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_version_action.GetSrcArtifactVersionRunPayload,
        run_context: get_src_artifact_version_action.GetSrcArtifactVersionRunContext,
    ) -> get_src_artifact_version_action.GetSrcArtifactVersionRunResult:
        src_artifact_def_path = payload.src_artifact_def_path

        src_artifact_raw_def = (
            await self.project_info_provider.get_project_raw_config(
                project_def_path=src_artifact_def_path
            )
        )

        # Check that version is dynamic
        dynamic_fields = src_artifact_raw_def.get("project", {}).get("dynamic", [])
        if "version" not in dynamic_fields:
            raise code_action.ActionFailedException(
                f"Version is not dynamic in {src_artifact_def_path}, "
                "this handler only supports dynamic versions via setuptools_scm"
            )

        # from setuptools_scm._cli:main
        pyproject = src_artifact_def_path.as_posix()

        try:
            # could be optimized by providing config from project_info_provider instead
            # of reading file each time
            config = Configuration.from_file(
                pyproject,
                root=None
            )
        except (LookupError, FileNotFoundError) as ex:
            # no pyproject.toml OR no [tool.setuptools_scm]
            self.logger.warning(
                f"Warning: could not use {pyproject},"
                " using default configuration.\n"
                f" Reason: {ex}."
            )
            config = Configuration(root=src_artifact_def_path.parent.as_posix())

        version = _get_version(
            config
        )
        if version is None:
            raise code_action.ActionFailedException("ERROR: no version found")

        # from setuptools_scm._cli:main end

        return get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
            version=version
        )
