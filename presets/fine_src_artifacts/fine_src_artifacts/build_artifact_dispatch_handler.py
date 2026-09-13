import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_src_artifacts import build_artifact_action, get_src_artifact_language_action


@dataclasses.dataclass
class BuildArtifactDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class BuildArtifactDispatchHandler(
    code_action.ActionHandler[
        build_artifact_action.BuildArtifactAction,
        BuildArtifactDispatchHandlerConfig,
    ]
):
    """Detect the artifact's language and dispatch to the language-specific build subaction.
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
        payload: build_artifact_action.BuildArtifactRunPayload,
        run_context: build_artifact_action.BuildArtifactRunContext,
    ) -> build_artifact_action.BuildArtifactRunResult:
        src_artifact_def_path = payload.src_artifact_def_path
        if src_artifact_def_path is None:
            src_artifact_def_path = path_to_resource_uri(
                self.project_info_provider.get_current_project_def_path()
            )

        language_result = await self.action_runner.run_action(
            action_type=iprojectactionrunner.ActionRef.from_type(
                get_src_artifact_language_action.GetSrcArtifactLanguageAction
            ),
            payload=get_src_artifact_language_action.GetSrcArtifactLanguageRunPayload(
                src_artifact_def_path=src_artifact_def_path,
            ),
            meta=run_context.meta,
        )
        language = language_result.language
        self.logger.debug(f"Detected language '{language}' for {src_artifact_def_path}")

        subactions_by_lang = await self.action_runner.get_actions_for_parent(
            build_artifact_action.BuildArtifactAction
        )
        if language not in subactions_by_lang:
            raise iprojectactionrunner.ActionNotFound(
                f"No build action registered for language '{language}'"
            )
        subaction = subactions_by_lang[language]
        return await self.action_runner.run_action(
            action_type=subaction,
            payload=dataclasses.replace(
                payload, src_artifact_def_path=src_artifact_def_path
            ),
            meta=run_context.meta,
        )
