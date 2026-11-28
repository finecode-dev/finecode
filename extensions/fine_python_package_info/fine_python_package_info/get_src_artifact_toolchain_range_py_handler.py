import dataclasses

from fine_src_artifacts import get_src_artifact_toolchain_range_action
from fine_src_artifacts.get_src_artifact_toolchain_range_action import (
    GetSrcArtifactToolchainRangeRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_python_package_info import requires_python as requires_python_utils


@dataclasses.dataclass
class GetSrcArtifactToolchainRangePyHandlerConfig(code_action.ActionHandlerConfig):
    min_version: str | None = None
    """Pin the oldest supported version, e.g. ``"3.11"``, instead of deriving it.

    A pin is for a project whose real floor is not what ``requires-python`` says -- an
    application deployed on one interpreter, say. Prefer fixing ``requires-python``:
    everything else (the interpreter axis, packaging metadata, consumers' resolvers)
    reads that, and a pin here moves only the tools."""
    max_version: str | None = None
    """Pin the newest supported version instead of deriving it. See ``min_version``."""


class GetSrcArtifactToolchainRangePyHandler(
    code_action.ActionHandler[
        get_src_artifact_toolchain_range_action.GetSrcArtifactToolchainRangeAction,
        GetSrcArtifactToolchainRangePyHandlerConfig,
    ]
):
    """Derive the supported toolchain range from ``project.requires-python``.

    This is the *default* algorithm, not a privileged one. A project that keeps its
    support range somewhere else -- a ``.python-version``, a company policy file --
    replaces this handler and every tool follows the new source at once, which is the
    point of the range being an action rather than each tool's own config field.
    """

    def __init__(
        self,
        config: GetSrcArtifactToolchainRangePyHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: get_src_artifact_toolchain_range_action.GetSrcArtifactToolchainRangeRunPayload,
        run_context: get_src_artifact_toolchain_range_action.GetSrcArtifactToolchainRangeRunContext,
    ) -> GetSrcArtifactToolchainRangeRunResult:
        if payload.src_artifact_def_path is not None:
            src_artifact_def_path = resource_uri_to_path(payload.src_artifact_def_path)
        else:
            src_artifact_def_path = (
                self.project_info_provider.get_current_project_def_path()
            )

        provenance: list[str] = []
        if self.config.min_version is not None:
            provenance.append("min_version pinned in handler config")
        if self.config.max_version is not None:
            provenance.append("max_version pinned in handler config")

        if self.config.min_version is not None and self.config.max_version is not None:
            # both ends pinned: requires-python cannot change the answer, so it is not
            # read at all and a project without one is not reported as missing anything
            return GetSrcArtifactToolchainRangeRunResult(
                min_version=self.config.min_version,
                max_version=self.config.max_version,
                derived_from="; ".join(provenance),
            )

        raw_config = await self.project_info_provider.get_project_raw_config(
            project_def_path=src_artifact_def_path
        )
        requires_python = raw_config.get("project", {}).get("requires-python", None)

        if requires_python is None:
            self.logger.warning(
                f"project.requires-python not found in {src_artifact_def_path}: there is"
                " no declared support range to derive from, so tools fall back to their"
                ' own defaults. Declare it (e.g. requires-python = ">=3.11") to pin'
                " down what they target."
            )
            derived_min: str | None = None
            derived_max: str | None = None
        else:
            derived_min, derived_max = requires_python_utils.support_range(
                str(requires_python)
            )
            provenance.append(
                f"project.requires-python ('{requires_python}')"
                f" of {src_artifact_def_path}"
            )

        return GetSrcArtifactToolchainRangeRunResult(
            min_version=self.config.min_version
            if self.config.min_version is not None
            else derived_min,
            max_version=self.config.max_version
            if self.config.max_version is not None
            else derived_max,
            derived_from="; ".join(provenance) if provenance else None,
        )
