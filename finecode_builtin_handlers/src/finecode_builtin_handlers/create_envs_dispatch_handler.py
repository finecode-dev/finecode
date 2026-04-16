import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.environments import (
    create_env_action,
    create_envs_action,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner


@dataclasses.dataclass
class CreateEnvsDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class CreateEnvsDispatchHandler(
    code_action.ActionHandler[
        create_envs_action.CreateEnvsAction, CreateEnvsDispatchHandlerConfig
    ]
):
    """Dispatch a create_env call per environment concurrently."""

    def __init__(
        self, action_runner: iprojectactionrunner.IProjectActionRunner, logger: ilogger.ILogger
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: create_envs_action.CreateEnvsRunPayload,
        run_context: create_envs_action.CreateEnvsRunContext,
    ) -> create_envs_action.CreateEnvsRunResult:
        if run_context.envs is None:
            raise code_action.ActionFailedException(
                "envs must be either provided in payload or be discovered by previous `create_envs` handlers"
            )

        tasks: list[asyncio.Task[create_envs_action.CreateEnvsRunResult]] = []
        async with run_context.progress("Creating environments", total=len(run_context.envs)) as progress:
            async def _create_and_advance(env):
                result = await self.action_runner.run_action(
                    action_type=create_env_action.CreateEnvAction,
                    payload=create_env_action.CreateEnvRunPayload(
                        env=env,
                        recreate=payload.recreate,
                    ),
                    meta=run_context.meta,
                )
                await progress.advance(message=f"Created {env.name}")
                return result

            try:
                async with asyncio.TaskGroup() as tg:
                    for env in run_context.envs:
                        tasks.append(tg.create_task(_create_and_advance(env)))
            except ExceptionGroup as eg:
                error_str = ". ".join([str(e) for e in eg.exceptions])
                raise code_action.ActionFailedException(error_str) from eg

            errors: list[str] = []
            for task in tasks:
                errors += task.result().errors
            return create_envs_action.CreateEnvsRunResult(errors=errors)
