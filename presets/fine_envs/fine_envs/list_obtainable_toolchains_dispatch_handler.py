import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from fine_envs import list_obtainable_toolchains_action
from fine_src_artifacts import get_src_artifact_language_action


@dataclasses.dataclass
class ListObtainableToolchainsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class ListObtainableToolchainsDispatchHandler(
    code_action.ActionHandler[
        list_obtainable_toolchains_action.ListObtainableToolchainsAction,
        ListObtainableToolchainsDispatchHandlerConfig,
    ]
):
    """Detect the project language and dispatch to its obtainable-toolchain source."""

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
        payload: list_obtainable_toolchains_action.ListObtainableToolchainsRunPayload,
        run_context: list_obtainable_toolchains_action.ListObtainableToolchainsRunContext,
    ) -> list_obtainable_toolchains_action.ListObtainableToolchainsRunResult:
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

        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            list_obtainable_toolchains_action.ListObtainableToolchainsAction
        )
        if language not in subactions_by_lang:
            raise iprojectactionrunner.ActionNotFound(
                f"No obtainable-toolchain source registered for language '{language}'"
            )

        return await self.action_runner.run_action(
            action_type=subactions_by_lang[language],
            payload=payload,
            meta=run_context.meta,
        )
