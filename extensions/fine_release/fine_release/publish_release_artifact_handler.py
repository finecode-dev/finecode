from __future__ import annotations

import dataclasses

from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunPayload,
)
from fine_src_artifacts.get_src_artifact_registries_action import (
    GetSrcArtifactRegistriesAction,
    GetSrcArtifactRegistriesRunPayload,
)
from finecode_dev_extensions.publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
    PublishAndVerifyArtifactRunPayload,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri

from fine_release.release_package_action import (
    RegistryPublishOutcome,
    RegistryPublishResult,
    ReleasePackageAction,
    ReleasePackageRunContext,
    ReleasePackageRunPayload,
    ReleasePackageRunResult,
    result_from_state,
)


@dataclasses.dataclass
class PublishReleaseArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class PublishReleaseArtifactHandler(
    code_action.ActionHandler[
        ReleasePackageAction,
        PublishReleaseArtifactHandlerConfig,
    ]
):
    """Publish the built artifacts to every configured registry and record a
    per-registry outcome for each.

    Dry-run resolves the same registries and asks each whether the version is
    already there, so a preview reflects the chain that would actually run.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.project_info = project_info
        self.logger = logger

    def _src_artifact_def_path(self, payload: ReleasePackageRunPayload) -> ResourceUri:
        # The sub-actions below all require a concrete path, while the payload
        # follows the build_artifact convention where None means "this
        # project's own artifact".
        if payload.src_artifact_def_path is not None:
            return payload.src_artifact_def_path
        return path_to_resource_uri(self.project_info.get_current_project_def_path())

    async def run(
        self,
        payload: ReleasePackageRunPayload,
        run_context: ReleasePackageRunContext,
    ) -> ReleasePackageRunResult:
        state = run_context.state

        if state.error is not None:
            return result_from_state(payload, state)

        src_artifact_def_path = self._src_artifact_def_path(payload)

        try:
            registries_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    GetSrcArtifactRegistriesAction
                ),
                payload=GetSrcArtifactRegistriesRunPayload(
                    src_artifact_def_path=src_artifact_def_path
                ),
                meta=run_context.meta,
            )
        except Exception as exception:
            state.error = (
                f"Could not resolve registries for {payload.package_name}: {exception}"
            )
            self.logger.error(state.error)
            return result_from_state(payload, state)

        state.registries = [registry.name for registry in registries_result.registries]
        if not state.registries:
            state.error = (
                f"No registries configured/resolved for {payload.package_name}; "
                "nothing to publish to."
            )
            self.logger.error(state.error)
            return result_from_state(payload, state)

        if payload.dry_run:
            await self._preview(payload, run_context, src_artifact_def_path)
        else:
            await self._publish(payload, run_context, src_artifact_def_path)

        return result_from_state(payload, state)

    async def _preview(
        self,
        payload: ReleasePackageRunPayload,
        run_context: ReleasePackageRunContext,
        src_artifact_def_path: ResourceUri,
    ) -> None:
        state = run_context.state
        registry_results: list[RegistryPublishResult] = []

        for registry_name in state.registries:
            # Per-registry, so one unreachable registry reports itself FAILED
            # without aborting the preview of the others.
            try:
                list_result = await self.action_runner.run_action(
                    action_type=iprojectactionrunner.ActionRef.from_type(
                        ListPublishedArtifactsAction
                    ),
                    payload=ListPublishedArtifactsRunPayload(
                        src_artifact_def_path=src_artifact_def_path,
                        version=payload.version,
                        registry_name=registry_name,
                    ),
                    meta=run_context.meta,
                )
            except Exception as exception:
                registry_results.append(
                    RegistryPublishResult(
                        registry=registry_name,
                        outcome=RegistryPublishOutcome.FAILED,
                        errors=[str(exception)],
                    )
                )
                continue

            registry_results.append(
                RegistryPublishResult(
                    registry=registry_name,
                    outcome=(
                        RegistryPublishOutcome.SKIPPED
                        if list_result.filenames
                        else RegistryPublishOutcome.WOULD_PUBLISH
                    ),
                )
            )

        state.registry_results = registry_results

    async def _publish(
        self,
        payload: ReleasePackageRunPayload,
        run_context: ReleasePackageRunContext,
        src_artifact_def_path: ResourceUri,
    ) -> None:
        state = run_context.state

        if not state.build_output_paths:
            # Defence in depth behind the state.error guard: nothing to upload
            # means the build never produced artifacts.
            state.error = (
                f"No built artifacts to publish for {payload.package_name} "
                f"{payload.version}."
            )
            self.logger.error(state.error)
            return

        try:
            publish_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    PublishAndVerifyArtifactAction
                ),
                payload=PublishAndVerifyArtifactRunPayload(
                    src_artifact_def_path=src_artifact_def_path,
                    dist_artifact_paths=state.build_output_paths,
                ),
                meta=run_context.meta,
            )
        except Exception as exception:
            # Failure before any registry was dispatched to. Name the resolved
            # registries in the message, but record no per-registry outcomes:
            # nothing was attempted against them, so there is nothing to report.
            state.error = (
                f"Publish failed for {payload.package_name} {payload.version} "
                f"before reaching any registry ({', '.join(state.registries)}): "
                f"{exception}"
            )
            self.logger.error(state.error)
            return

        registry_results: list[RegistryPublishResult] = []
        for registry_name in state.registries:
            if registry_name in publish_result.publish_errors:
                registry_results.append(
                    RegistryPublishResult(
                        registry=registry_name,
                        outcome=RegistryPublishOutcome.FAILED,
                        errors=publish_result.publish_errors[registry_name],
                    )
                )
            elif registry_name in publish_result.verification_errors:
                registry_results.append(
                    RegistryPublishResult(
                        registry=registry_name,
                        outcome=RegistryPublishOutcome.FAILED,
                        errors=publish_result.verification_errors[registry_name],
                    )
                )
            elif registry_name in publish_result.published_registries:
                registry_results.append(
                    RegistryPublishResult(
                        registry=registry_name,
                        outcome=RegistryPublishOutcome.PUBLISHED,
                    )
                )
            else:
                registry_results.append(
                    RegistryPublishResult(
                        registry=registry_name,
                        outcome=RegistryPublishOutcome.SKIPPED,
                    )
                )

        state.registry_results = registry_results
