import dataclasses
import shutil

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_runners as prepare_runners_action
from finecode_extension_api.interfaces import iactionrunner, iprojectinfoprovider, ilogger


@dataclasses.dataclass
class PrepareRunnersDumpConfigsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareRunnersDumpConfigsHandler(
    code_action.ActionHandler[prepare_runners_action.PrepareRunnersAction, PrepareRunnersDumpConfigsHandlerConfig]
):
    def __init__(self, action_runner: iactionrunner.IActionRunner, project_info_provider: iprojectinfoprovider.IProjectInfoProvider, logger: ilogger.ILogger) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger
    
    async def run(
        self, payload: prepare_runners_action.PrepareRunnersRunPayload, run_context: prepare_runners_action.PrepareRunnersRunContext
    ) -> prepare_runners_action.PrepareRunnersRunResult:
        project_defs_pathes = set([env_info.project_def_path for env_info in payload.envs])
        if len(project_defs_pathes) != 1:
            raise code_action.ActionFailedException("prepare_envs action currently supports only preparing environments in the same project where it is running(dump_configs handler)")
        
        project_raw_config = await self.project_info_provider.get_project_raw_config()
        
        project_def_path = project_defs_pathes.pop()
        project_dir_path = project_def_path.parent
        # TODO: unify with call of dump_config in CLI
        dump_dir_path = project_dir_path / 'finecode_config_dump'
        try:
            dump_config_result = await self.action_runner.run_action(name='dump_config', payload={
                "source_file_path": project_def_path,
                "project_raw_config": project_raw_config,
                "target_file_path": dump_dir_path / 'pyproject.toml'
            })
            new_project_def_path = dump_dir_path / 'pyproject.toml'
            for env_info in payload.envs:
                run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = new_project_def_path
                run_context.project_def_by_venv_dir_path[env_info.venv_dir_path] = dump_config_result['config_dump']
        except iactionrunner.BaseRunActionException as exception:
            raise code_action.ActionFailedException(f"Running 'dump_config' action as part of 'prepare_envs' failed: {type(exception)}, {exception.message}")

        return prepare_runners_action.PrepareRunnersRunResult(errors=[])
