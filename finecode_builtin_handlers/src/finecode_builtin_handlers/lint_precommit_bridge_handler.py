import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import lint_action, precommit_action
from finecode_extension_api.interfaces import iactionrunner, ilogger
from finecode_extension_api.resource_uri import path_to_resource_uri


@dataclasses.dataclass
class LintPrecommitBridgeHandlerConfig(code_action.ActionHandlerConfig): ...


class LintPrecommitBridgeHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, LintPrecommitBridgeHandlerConfig
    ]
):
    """Bridge handler that runs lint on the staged files."""

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: precommit_action.PrecommitRunPayload,
        run_context: precommit_action.PrecommitRunContext,
    ) -> precommit_action.PrecommitRunResult:
        if run_context.staged_files is None:
            raise code_action.ActionFailedException(
                "discovery handler must be registered before bridge handlers"
            )
        if not run_context.staged_files:
            self.logger.info("No staged files - skipping lint.")
            return precommit_action.PrecommitRunResult()

        file_uris = [path_to_resource_uri(p) for p in run_context.staged_files]
        lint_action_instance = self.action_runner.get_action_by_source(
            lint_action.LintAction
        )
        result = await self.action_runner.run_action(
            action=lint_action_instance,
            payload=lint_action.LintRunPayload(
                target=lint_action.LintTarget.FILES,
                file_paths=file_uris,
            ),
            meta=run_context.meta,
        )
        return precommit_action.PrecommitRunResult(action_results={"lint": result})
