# docs: docs/reference/actions.md
import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.resource_uri import ResourceUri
from finecode_extension_api.actions.artifact import (
    get_src_artifact_registries_action,
    get_src_artifact_version_action,
)
from finecode_extension_api.actions.publishing import (
    is_artifact_published_to_registry_action,
    publish_artifact_action,
    publish_artifact_to_registry_action,
)
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iprojectinfoprovider,
)


@dataclasses.dataclass
class PublishArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class PublishArtifactHandler(
    code_action.ActionHandler[
        publish_artifact_action.PublishArtifactAction,
        PublishArtifactHandlerConfig,
    ]
):
    action_runner: iactionrunner.IActionRunner
    logger: ilogger.ILogger
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: publish_artifact_action.PublishArtifactRunPayload,
        run_context: publish_artifact_action.PublishArtifactRunContext,
    ) -> publish_artifact_action.PublishArtifactRunResult:
        run_meta = run_context.meta

        src_artifact_def_path = payload.src_artifact_def_path
        dist_artifact_paths = payload.dist_artifact_paths

        async with run_context.progress("Publishing artifact") as progress:
            await progress.report("Getting artifact version")
            get_version_action = self.action_runner.get_action_by_source(
                get_src_artifact_version_action.GetSrcArtifactVersionAction
            )
            version_payload = (
                get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
                    src_artifact_def_path=src_artifact_def_path
                )
            )
            version_result = await self.action_runner.run_action(
                action=get_version_action, payload=version_payload, meta=run_meta
            )
            version = version_result.version

            get_registries_action = self.action_runner.get_action_by_source(
                get_src_artifact_registries_action.GetSrcArtifactRegistriesAction
            )
            registries_payload = (
                get_src_artifact_registries_action.GetSrcArtifactRegistriesRunPayload(
                    src_artifact_def_path=src_artifact_def_path
                )
            )
            registries_result = await self.action_runner.run_action(
                action=get_registries_action, payload=registries_payload, meta=run_meta
            )

            # Filter registries based on publication status if not forced
            registries_to_publish = registries_result.registries
            if len(registries_to_publish) == 0:
                raise code_action.ActionFailedException("No registries are configured")

            # Build dict of paths to publish per registry
            dist_paths_to_publish_by_registry: dict[str, list[ResourceUri]]
            if payload.force:
                dist_paths_to_publish_by_registry = {
                    registry.name: dist_artifact_paths
                    for registry in registries_to_publish
                }
            else:
                await progress.report("Checking publication status")
                is_published_action = self.action_runner.get_action_by_source(
                    is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryAction
                )

                check_tasks: list[tuple[asyncio.Task[is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunResult], get_src_artifact_registries_action.Registry]] = []
                try:
                    async with asyncio.TaskGroup() as tg:
                        for registry in registries_result.registries:
                            check_payload = is_artifact_published_to_registry_action.IsArtifactPublishedToRegistryRunPayload(
                                src_artifact_def_path=src_artifact_def_path,
                                dist_artifact_paths=dist_artifact_paths,
                                version=version,
                                registry_name=registry.name,
                            )
                            task = tg.create_task(
                                self.action_runner.run_action(
                                    action=is_published_action,
                                    payload=check_payload,
                                    meta=run_meta,
                                )
                            )
                            check_tasks.append((task, registry))
                except ExceptionGroup as eg:
                    error_str = ". ".join([str(exception) for exception in eg.exceptions])
                    raise code_action.ActionFailedException(error_str) from eg

                # Filter to only dist paths that are not published per registry
                dist_paths_to_publish_by_registry = {}
                for task, registry in check_tasks:
                    result = task.result()
                    not_published_paths = [
                        path
                        for path, is_published in result.is_published_by_dist_path.items()
                        if not is_published
                    ]
                    if not_published_paths:
                        dist_paths_to_publish_by_registry[registry.name] = not_published_paths

            # Publish to registries with unpublished artifacts
            await progress.report("Publishing to registries")
            publish_to_registry_action = self.action_runner.get_action_by_source(
                publish_artifact_to_registry_action.PublishArtifactToRegistryAction
            )

            publish_tasks: list[asyncio.Task[publish_artifact_to_registry_action.PublishArtifactToRegistryRunResult]] = []
            try:
                async with asyncio.TaskGroup() as tg:
                    for registry_name, paths_to_publish in dist_paths_to_publish_by_registry.items():
                        publish_payload = publish_artifact_to_registry_action.PublishArtifactToRegistryRunPayload(
                            src_artifact_def_path=src_artifact_def_path,
                            dist_artifact_paths=paths_to_publish,
                            registry_name=registry_name,
                            force=payload.force,
                        )
                        task = tg.create_task(
                            self.action_runner.run_action(
                                action=publish_to_registry_action,
                                payload=publish_payload,
                                meta=run_meta,
                            )
                        )
                        publish_tasks.append(task)
            except ExceptionGroup as eg:
                error_str = ". ".join([str(exception) for exception in eg.exceptions])
                raise code_action.ActionFailedException(error_str) from eg

            published_registries = list(dist_paths_to_publish_by_registry.keys())

            return publish_artifact_action.PublishArtifactRunResult(
                version=version, published_registries=published_registries
            )
