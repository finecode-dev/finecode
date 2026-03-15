import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions import (
    prepare_runner_env as prepare_runner_env_action,
    prepare_runner_envs as prepare_runner_envs_action,
)
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class PrepareRunnerEnvsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnerEnvsDispatchHandler(
    code_action.ActionHandler[
        prepare_runner_envs_action.PrepareRunnerEnvsAction,
        PrepareRunnerEnvsDispatchHandlerConfig,
    ]
):
    """Dispatch a prepare_runner_env call per environment concurrently."""

    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: prepare_runner_envs_action.PrepareRunnerEnvsRunPayload,
        run_context: prepare_runner_envs_action.PrepareRunnerEnvsRunContext,
    ) -> prepare_runner_envs_action.PrepareRunnerEnvsRunResult:
        prepare_runner_env_action_instance = self.action_runner.get_action_by_name(
            name="prepare_runner_env",
            expected_type=prepare_runner_env_action.PrepareRunnerEnvAction,
        )

        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be provided in payload or by previous handlers"
            )
        tasks: list[
            asyncio.Task[prepare_runner_envs_action.PrepareRunnerEnvsRunResult]
        ] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for env in run_context.envs:
                    task = tg.create_task(
                        self.action_runner.run_action(
                            action=prepare_runner_env_action_instance,
                            payload=prepare_runner_env_action.PrepareRunnerEnvRunPayload(
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
        return prepare_runner_envs_action.PrepareRunnerEnvsRunResult(errors=errors)
