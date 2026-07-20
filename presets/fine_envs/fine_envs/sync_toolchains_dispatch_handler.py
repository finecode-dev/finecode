import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from fine_envs import sync_toolchains_action
from fine_src_artifacts import get_src_artifact_language_action


@dataclasses.dataclass
class SyncToolchainsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class SyncToolchainsDispatchHandler(
    code_action.ActionHandler[
        sync_toolchains_action.SyncToolchainsAction,
        SyncToolchainsDispatchHandlerConfig,
    ]
):
    """Detect the project language and dispatch to its toolchain-sourcing subaction.

    Each language materializes the axis differently — Python expands
    ``requires-python`` into ``interpreters`` — so only the language subaction knows
    both the support range and the config key it lands in. The subaction must be
    registered and its payload must extend SyncToolchainsRunPayload (extra fields
    must have defaults).
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: sync_toolchains_action.SyncToolchainsRunPayload,
        run_context: sync_toolchains_action.SyncToolchainsRunContext,
    ) -> sync_toolchains_action.SyncToolchainsRunResult:
        project_def_path = payload.project_def_path
        if project_def_path is None:
            project_def_path = path_to_resource_uri(
                self.project_info_provider.get_current_project_def_path()
            )

        language_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                get_src_artifact_language_action.GetSrcArtifactLanguageAction
            ),
            payload=get_src_artifact_language_action.GetSrcArtifactLanguageRunPayload(
                src_artifact_def_path=project_def_path,
            ),
            meta=run_context.meta,
        )
        language = language_result.language
        self.logger.debug(
            f"Detected language '{language}' for {project_def_path}"
        )

        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            sync_toolchains_action.SyncToolchainsAction
        )
        if language not in subactions_by_lang:
            raise iprojectactionrunner.ActionNotFound(
                f"No toolchain source registered for language '{language}'"
            )

        return await self.action_runner.run_action(
            action_type=subactions_by_lang[language],
            payload=sync_toolchains_action.SyncToolchainsRunPayload(
                project_def_path=project_def_path,
                save=payload.save,
            ),
            meta=run_context.meta,
        )
