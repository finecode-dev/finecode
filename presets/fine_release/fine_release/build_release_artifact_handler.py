from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from fine_src_artifacts.build_artifact_action import (
    BuildArtifactAction,
    BuildArtifactRunPayload,
)

from fine_release.release_package_action import (
    ReleasePackageAction,
    ReleasePackageRunContext,
    ReleasePackageRunPayload,
    ReleasePackageRunResult,
    result_from_state,
)


@dataclasses.dataclass
class BuildReleaseArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class BuildReleaseArtifactHandler(
    code_action.ActionHandler[
        ReleasePackageAction,
        BuildReleaseArtifactHandlerConfig,
    ]
):
    """Build the distribution artifacts the publish step uploads."""

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: ReleasePackageRunPayload,
        run_context: ReleasePackageRunContext,
    ) -> ReleasePackageRunResult:
        state = run_context.state

        if state.error is not None:
            return result_from_state(payload, state)

        if payload.dry_run:
            return result_from_state(payload, state)

        try:
            build_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(BuildArtifactAction),
                payload=BuildArtifactRunPayload(
                    src_artifact_def_path=payload.src_artifact_def_path
                ),
                meta=run_context.meta,
            )
        except Exception as exception:
            state.error = (
                f"Build failed for {payload.package_name} {payload.version}: {exception}"
            )
            self.logger.error(state.error)
            return result_from_state(payload, state)

        state.build_output_paths = list(build_result.build_output_paths)
        self.logger.debug(
            f"Built {payload.package_name} {payload.version}: "
            f"{len(state.build_output_paths)} artifact(s)"
        )

        return result_from_state(payload, state)
