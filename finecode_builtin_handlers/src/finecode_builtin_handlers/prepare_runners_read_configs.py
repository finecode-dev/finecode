import asyncio
import dataclasses
import pathlib
import shutil
import typing

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_runners as prepare_runners_action
from finecode_extension_api.interfaces import (
    iactionrunner,
    ilogger,
    iprojectinfoprovider,
)
from finecode_builtin_handlers import dependency_config_utils


@dataclasses.dataclass
class PrepareRunnersReadConfigsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnersReadConfigsHandler(
    code_action.ActionHandler[
        prepare_runners_action.PrepareRunnersAction,
        PrepareRunnersReadConfigsHandlerConfig,
    ]
):
    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger

    async def run(
        self,
        payload: prepare_runners_action.PrepareRunnersRunPayload,
        run_context: prepare_runners_action.PrepareRunnersRunContext,
    ) -> prepare_runners_action.PrepareRunnersRunResult:
        project_defs_pathes = set(
            [env_info.project_def_path for env_info in payload.envs]
        )
        raw_config_by_project_def_path: dict[pathlib.Path, dict[str, typing.Any]] = {}

        get_config_tasks: list[asyncio.Task] = []
        async with asyncio.TaskGroup() as tg:
            for project_def_path in project_defs_pathes:
                task = tg.create_task(
                    self.project_info_provider.get_project_raw_config(project_def_path)
                )
                get_config_tasks.append(task)

        for idx, project_def_path in enumerate(project_defs_pathes):
            project_raw_config = get_config_tasks[idx].result()
            dependency_config_utils.make_project_config_pip_compatible(
                project_raw_config, project_def_path
            )
            raw_config_by_project_def_path[project_def_path] = project_raw_config

        for env_info in payload.envs:
            run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = (
                env_info.project_def_path
            )
            project_raw_config = raw_config_by_project_def_path[
                env_info.project_def_path
            ]
            run_context.project_def_by_venv_dir_path[env_info.venv_dir_path] = (
                project_raw_config
            )

        return prepare_runners_action.PrepareRunnersRunResult(errors=[])
