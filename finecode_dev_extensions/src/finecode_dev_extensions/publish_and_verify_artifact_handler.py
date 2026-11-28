import asyncio
import dataclasses
import typing

from fine_dist_artifacts import (
    publish_artifact_action,
    verify_artifact_published_to_registry_action,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner, iprojectinfoprovider

from .publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
    PublishAndVerifyArtifactRunContext,
    PublishAndVerifyArtifactRunPayload,
    PublishAndVerifyArtifactRunResult,
)


@dataclasses.dataclass
class PublishAndVerifyArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class PublishAndVerifyArtifactHandler(
    code_action.ActionHandler[
        PublishAndVerifyArtifactAction,
        PublishAndVerifyArtifactHandlerConfig,
    ]
):
    action_runner: iprojectactionrunner.IProjectActionRunner
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider

    async def _verify_registry(
        self,
        payload: PublishAndVerifyArtifactRunPayload,
        version: str,
        registry_name: str,
        meta: code_action.RunActionMeta,
    ) -> list[str]:
        try:
            verify_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryAction
                ),
                payload=verify_artifact_published_to_registry_action.VerifyArtifactPublishedToRegistryRunPayload(
                    src_artifact_def_path=payload.src_artifact_def_path,
                    dist_artifact_paths=payload.dist_artifact_paths,
                    registry_name=registry_name,
                    version=version,
                ),
                meta=meta,
            )
        except iprojectactionrunner.ActionRunCancelled:
            raise
        except Exception as exception:  # noqa: BLE001 -- any verification failure is this registry's error
            errors = [str(exception)]
        else:
            errors = verify_result.errors
        return [
            f"{registry_name}: Upload accepted but not confirmed: {error}"
            for error in errors
        ]

    async def run(
        self,
        payload: PublishAndVerifyArtifactRunPayload,
        run_context: PublishAndVerifyArtifactRunContext,
    ) -> PublishAndVerifyArtifactRunResult:
        run_meta = run_context.meta

        src_artifact_def_path = payload.src_artifact_def_path
        dist_artifact_paths = payload.dist_artifact_paths

        # Publish the artifact
        publish_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                publish_artifact_action.PublishArtifactAction
            ),
            payload=publish_artifact_action.PublishArtifactRunPayload(
                src_artifact_def_path=src_artifact_def_path,
                dist_artifact_paths=dist_artifact_paths,
                force=payload.force,
            ),
            meta=run_meta,
        )
        published_registries = publish_result.published_registries
        publish_errors = {
            registry_name: [error]
            for registry_name, error in publish_result.failed_registries.items()
        }

        version = publish_result.version

        checks = [
            asyncio.create_task(
                self._verify_registry(payload, version, registry_name, run_meta)
            )
            for registry_name in published_registries
        ]
        verifications = asyncio.gather(*checks)
        try:
            # Not `await verifications`: a cancelled `gather` waits for every check to
            # acknowledge cancellation, and a cancelled run must re-raise at once.
            _ = await asyncio.wait({verifications})
        except asyncio.CancelledError:
            _ = verifications.cancel()
            verifications.add_done_callback(_retrieve_late_outcome)
            raise
        try:
            per_registry_errors = verifications.result()
        except (asyncio.CancelledError, iprojectactionrunner.ActionRunCancelled):
            # A check was cancelled: abandon the others. `gather` retrieves their
            # late outcomes because it is already done.
            for check in checks:
                _ = check.cancel()
            raise
        verification_errors = {
            registry_name: errors
            for registry_name, errors in zip(
                published_registries, per_registry_errors, strict=True
            )
            if errors
        }

        return PublishAndVerifyArtifactRunResult(
            version=version,
            published_registries=published_registries,
            verification_errors=verification_errors,
            publish_errors=publish_errors,
        )


def _retrieve_late_outcome(future: asyncio.Future[typing.Any]) -> None:
    # Retrieved so the event loop does not report it as never retrieved.
    if not future.cancelled():
        _ = future.exception()
