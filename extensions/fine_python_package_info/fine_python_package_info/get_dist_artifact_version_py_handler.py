import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import \
    get_dist_artifact_version as get_dist_artifact_version_action
from finecode_extension_api.interfaces import ilogger


@dataclasses.dataclass
class GetDistArtifactVersionPyHandlerConfig(code_action.ActionHandlerConfig): ...


class GetDistArtifactVersionPyHandler(
    code_action.ActionHandler[
        get_dist_artifact_version_action.GetDistArtifactVersionAction,
        GetDistArtifactVersionPyHandlerConfig,
    ]
):
    def __init__(
        self,
        config: GetDistArtifactVersionPyHandlerConfig,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.logger = logger

    async def run(
        self,
        payload: get_dist_artifact_version_action.GetDistArtifactVersionRunPayload,
        run_context: get_dist_artifact_version_action.GetDistArtifactVersionRunContext,
    ) -> get_dist_artifact_version_action.GetDistArtifactVersionRunResult:
        filename = payload.dist_artifact_path.name
        version = self._extract_version_from_filename(filename)

        if version is None:
            raise code_action.ActionFailedException(
                f"Could not extract version from dist filename: {filename}"
            )

        return get_dist_artifact_version_action.GetDistArtifactVersionRunResult(
            version=version
        )

    def _extract_version_from_filename(self, filename: str) -> str | None:
        if filename.endswith('.whl'):
            # Wheel: name-version-python-abi-platform.whl
            parts = filename[:-4].split('-')
            if len(parts) >= 5:
                return parts[1]
        elif filename.endswith('.tar.gz'):
            # Source dist: name-version.tar.gz
            parts = filename[:-7].split('-')
            if len(parts) >= 2:
                return parts[1]
        elif filename.endswith('.zip'):
            # Source dist: name-version.zip
            parts = filename[:-4].split('-')
            if len(parts) >= 2:
                return parts[1]
        return None
