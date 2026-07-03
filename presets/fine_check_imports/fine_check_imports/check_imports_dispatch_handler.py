import dataclasses

from finecode_extension_api import code_action
from fine_check_imports import check_imports_action
from fine_src_artifacts import get_src_artifact_language_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner, iprojectinfoprovider


@dataclasses.dataclass
class CheckImportsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class CheckImportsDispatchHandler(
    code_action.ActionHandler[
        check_imports_action.CheckImportsAction,
        CheckImportsDispatchHandlerConfig,
    ]
):
    """Detect the project language and dispatch to the appropriate language-specific check action.

    The subaction name is derived by convention: language "python" maps to
    "check_python_imports", etc. The subaction must be registered and its
    payload must extend CheckImportsRunPayload (extra fields must have
    defaults).
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
        payload: check_imports_action.CheckImportsRunPayload,
        run_context: check_imports_action.CheckImportsRunContext,
    ) -> check_imports_action.CheckImportsRunResult:
        src_artifact_def_path = payload.src_artifact_def_path
        if src_artifact_def_path is None:
            src_artifact_def_path = self.project_info_provider.get_current_project_def_path()

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
            check_imports_action.CheckImportsAction
        )
        if language not in subactions_by_lang:
            self.logger.debug(
                f"No check_imports action registered for language '{language}' — skipping."
            )
            return check_imports_action.CheckImportsRunResult(messages={})
        subaction = subactions_by_lang[language]
        if subaction.action_type is None:
            self.logger.debug(
                f"check_imports action for language '{language}' is not locally importable — skipping."
            )
            return check_imports_action.CheckImportsRunResult(messages={})
        resolved_payload = dataclasses.replace(payload, src_artifact_def_path=src_artifact_def_path)
        subpayload = subaction.action_type.PAYLOAD_TYPE(**dataclasses.asdict(resolved_payload))
        return await self.action_runner.run_action(
            action_type=subaction,
            payload=subpayload,
            meta=run_context.meta,
        )
