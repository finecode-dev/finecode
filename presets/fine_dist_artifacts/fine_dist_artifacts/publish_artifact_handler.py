# docs: docs/reference/actions.md
import asyncio
import dataclasses

from fine_dist_artifacts import (
    list_published_artifacts_action,
    publish_artifact_action,
    publish_artifact_to_registry_action,
)
from fine_src_artifacts import (
    get_src_artifact_registries_action,
    get_src_artifact_version_action,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path


@dataclasses.dataclass
class PublishArtifactHandlerConfig(code_action.ActionHandlerConfig): ...


class PublishArtifactHandler(
    code_action.ActionHandler[
        publish_artifact_action.PublishArtifactAction,
        PublishArtifactHandlerConfig,
    ]
):
    action_runner: iprojectactionrunner.IProjectActionRunner
    logger: ilogger.ILogger
    project_info_provider: iprojectinfoprovider.IProjectInfoProvider

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
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
            version_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    get_src_artifact_version_action.GetSrcArtifactVersionAction
                ),
                payload=get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
                    src_artifact_def_path=src_artifact_def_path
                ),
                meta=run_meta,
            )
            version = version_result.version

            registries_result = await self.action_runner.run_action(
                action_type=iprojectactionrunner.ActionRef.from_type(
                    get_src_artifact_registries_action.GetSrcArtifactRegistriesAction
                ),
                payload=get_src_artifact_registries_action.GetSrcArtifactRegistriesRunPayload(
                    src_artifact_def_path=src_artifact_def_path
                ),
                meta=run_meta,
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
                check_tasks: list[
                    tuple[
                        asyncio.Task[
                            list_published_artifacts_action.ListPublishedArtifactsRunResult
                        ],
                        get_src_artifact_registries_action.Registry,
                    ]
                ] = []
                try:
                    async with asyncio.TaskGroup() as tg:
                        for registry in registries_result.registries:
                            check_payload = list_published_artifacts_action.ListPublishedArtifactsRunPayload(
                                src_artifact_def_path=src_artifact_def_path,
                                version=version,
                                registry_name=registry.name,
                            )
                            task = tg.create_task(
                                self.action_runner.run_action(
                                    action_type=iprojectactionrunner.ActionRef.from_type(
                                        list_published_artifacts_action.ListPublishedArtifactsAction
                                    ),
                                    payload=check_payload,
                                    meta=run_meta,
                                )
                            )
                            check_tasks.append((task, registry))
                except ExceptionGroup as eg:
                    error_str = ". ".join(
                        [str(exception) for exception in eg.exceptions]
                    )
                    raise code_action.ActionFailedException(error_str) from eg

                # Filter to only dist paths that are not published per registry
                dist_paths_to_publish_by_registry = {}
                for task, registry in check_tasks:
                    result = task.result()
                    published_filenames = set(result.filenames)
                    not_published_paths = [
                        path
                        for path in dist_artifact_paths
                        if resource_uri_to_path(path).name not in published_filenames
                    ]
                    if not_published_paths:
                        dist_paths_to_publish_by_registry[registry.name] = (
                            not_published_paths
                        )

            # Publish to registries with unpublished artifacts.
            #
            # Registries are independent publish targets, so one of them failing
            # must not cancel uploads already in flight to the others — a
            # TaskGroup here would do exactly that. publish_artifact_to_registry
            # reports its own failures in the result, so the expected failure path
            # is `result.error`; return_exceptions=True additionally keeps an
            # unexpected raise (ER crash, cancellation) attributable to one
            # registry instead of losing every other registry's outcome with it.
            await progress.report("Publishing to registries")
            registry_names = list(dist_paths_to_publish_by_registry.keys())
            publish_results = await asyncio.gather(
                *[
                    self.action_runner.run_action(
                        action_type=iprojectactionrunner.ActionRef.from_type(
                            publish_artifact_to_registry_action.PublishArtifactToRegistryAction
                        ),
                        payload=publish_artifact_to_registry_action.PublishArtifactToRegistryRunPayload(
                            src_artifact_def_path=src_artifact_def_path,
                            dist_artifact_paths=paths_to_publish,
                            registry_name=registry_name,
                            force=payload.force,
                        ),
                        meta=run_meta,
                    )
                    for registry_name, paths_to_publish in dist_paths_to_publish_by_registry.items()
                ],
                return_exceptions=True,
            )

            published_registries: list[str] = []
            failed_registries: dict[str, str] = {}
            for registry_name, publish_result in zip(registry_names, publish_results):
                if isinstance(publish_result, BaseException):
                    self.logger.error(
                        f"Publishing to {registry_name} raised: {publish_result}"
                    )
                    failed_registries[registry_name] = str(publish_result)
                elif publish_result.error is not None:
                    failed_registries[registry_name] = publish_result.error
                else:
                    published_registries.append(registry_name)

            return publish_artifact_action.PublishArtifactRunResult(
                version=version,
                published_registries=published_registries,
                failed_registries=failed_registries,
            )
