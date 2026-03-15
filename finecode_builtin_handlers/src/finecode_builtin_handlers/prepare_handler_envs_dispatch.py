import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    prepare_handler_env as prepare_handler_env_action,
    prepare_handler_envs as prepare_handler_envs_action,
)
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class PrepareHandlerEnvsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareHandlerEnvsDispatchHandler(
    code_action.ActionHandler[
        prepare_handler_envs_action.PrepareHandlerEnvsAction,
        PrepareHandlerEnvsDispatchHandlerConfig,
    ]
):
    """Dispatch a prepare_handler_env call per environment concurrently."""

    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_handler_envs_action.PrepareHandlerEnvsRunPayload,
        run_context: prepare_handler_envs_action.PrepareHandlerEnvsRunContext,
    ) -> prepare_handler_envs_action.PrepareHandlerEnvsRunResult:
        prepare_handler_env_action_instance = self.action_runner.get_action_by_name(
            name="prepare_handler_env",
            expected_type=prepare_handler_env_action.PrepareHandlerEnvAction,
        )

        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be populated must be provided in payload or populated by previous handlers"
            )
        tasks: list[
            asyncio.Task[prepare_handler_envs_action.PrepareHandlerEnvsRunResult]
        ] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for env in run_context.envs:
                    task = tg.create_task(
                        self.action_runner.run_action(
                            action=prepare_handler_env_action_instance,
                            payload=prepare_handler_env_action.PrepareHandlerEnvRunPayload(
                                env=env,
                            ),
                            meta=run_context.meta,
                        )
                    )
                    tasks.append(task)
        except ExceptionGroup as eg:
            error_str = ". ".join([str(e) for e in eg.exceptions])
            raise code_action.ActionFailedException(error_str) from eg

        errors: list[str] = []
        for task in tasks:
            errors += task.result().errors
        return prepare_handler_envs_action.PrepareHandlerEnvsRunResult(errors=errors)
