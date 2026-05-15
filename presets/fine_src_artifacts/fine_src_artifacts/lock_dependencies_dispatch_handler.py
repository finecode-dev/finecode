import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import get_src_artifact_language_action, lock_dependencies_action
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class LockDependenciesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class LockDependenciesDispatchHandler(
    code_action.ActionHandler[
        lock_dependencies_action.LockDependenciesAction,
        LockDependenciesDispatchHandlerConfig,
    ]
):
    """Detect the project language and dispatch to the appropriate language-specific lock action.

    The subaction name is derived by convention: language "python" maps to
    "lock_python_dependencies", "node" maps to "lock_node_dependencies", etc.
    The subaction must be registered and its payload must extend
    LockDependenciesRunPayload (extra fields must have defaults).
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: lock_dependencies_action.LockDependenciesRunPayload,
        run_context: lock_dependencies_action.LockDependenciesRunContext,
    ) -> lock_dependencies_action.LockDependenciesRunResult:
        language_result = await self.action_runner.run_action(
            action_type=get_src_artifact_language_action.GetSrcArtifactLanguageAction,
            payload=get_src_artifact_language_action.GetSrcArtifactLanguageRunPayload(
                src_artifact_def_path=payload.src_artifact_def_path,
            ),
            meta=run_context.meta,
        )
        language = language_result.language
        self.logger.debug(f"Detected language '{language}' for {payload.src_artifact_def_path}")

        subactions_by_lang = self.action_runner.get_actions_for_parent(
            lock_dependencies_action.LockDependenciesAction
        )
        if language not in subactions_by_lang:
            raise iprojectactionrunner.ActionNotFound(
                f"No lock action registered for language '{language}'"
            )
        subaction = subactions_by_lang[language]
        subpayload = subaction.PAYLOAD_TYPE(**dataclasses.asdict(payload))
        return await self.action_runner.run_action(
            action_type=subaction,
            payload=subpayload,
            meta=run_context.meta,
        )
