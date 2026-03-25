import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import (
    install_env_action,
    install_envs_action,
)
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class InstallEnvsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class InstallEnvsDispatchHandler(
    code_action.ActionHandler[
        install_envs_action.InstallEnvsAction,
        InstallEnvsDispatchHandlerConfig,
    ]
):
    """Dispatch an install_env call per environment concurrently."""

    def __init__(
        self, action_runner: iactionrunner.IActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: install_envs_action.InstallEnvsRunPayload,
        run_context: install_envs_action.InstallEnvsRunContext,
    ) -> install_envs_action.InstallEnvsRunResult:
        install_env_action_instance = self.action_runner.get_action_by_source(
            install_env_action.InstallEnvAction,
        )

        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be populated must be provided in payload or populated by previous handlers"
            )
        tasks: list[
            asyncio.Task[install_envs_action.InstallEnvsRunResult]
        ] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for env in run_context.envs:
                    task = tg.create_task(
                        self.action_runner.run_action(
                            action=install_env_action_instance,
                            payload=install_env_action.InstallEnvRunPayload(
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
        return install_envs_action.InstallEnvsRunResult(errors=errors)
