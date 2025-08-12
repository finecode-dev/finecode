import dataclasses
import shutil

import tomlkit

from finecode_extension_api import code_action
from finecode_extension_api.actions import prepare_envs as prepare_envs_action
from finecode_extension_api.interfaces import iactionrunner, iprojectinfoprovider, ilogger


@dataclasses.dataclass
class PrepareEnvsDumpConfigsHandlerConfig(code_action.ActionHandlerConfig): ...


class PrepareEnvsDumpConfigsHandler(
    code_action.ActionHandler[prepare_envs_action.PrepareEnvsAction, PrepareEnvsDumpConfigsHandlerConfig]
):
    def __init__(self, action_runner: iactionrunner.IActionRunner, project_info_provider: iprojectinfoprovider.IProjectInfoProvider, logger: ilogger.ILogger) -> None:
        self.action_runner = action_runner
        self.project_info_provider = project_info_provider
        self.logger = logger
    
    async def run(
        self, payload: prepare_envs_action.PrepareEnvsRunPayload, run_context: prepare_envs_action.PrepareEnvsRunContext
    ) -> prepare_envs_action.PrepareEnvsRunResult:
        project_defs_pathes = set([env_info.project_def_path for env_info in payload.envs])
        if len(project_defs_pathes) != 1:
            ... # TODO: error
        
        project_raw_config = await self.project_info_provider.get_project_raw_config()
        
        project_def_path = project_defs_pathes.pop()
        project_dir_path = project_def_path.parent
        # TODO: unify with call of dump_config in CLI
        dump_dir_path = project_dir_path / 'finecode_config_dump'
        try:
            await self.action_runner.run_action(name='dump_config', payload={
                "source_file_path": project_def_path,
                "project_raw_config": project_raw_config,
                "target_file_path": dump_dir_path / 'pyproject.toml'
            })
            new_project_def_path = dump_dir_path / 'pyproject.toml'
            for env_info in payload.envs:
                run_context.project_def_path_by_venv_dir_path[env_info.venv_dir_path] = new_project_def_path
        except iactionrunner.BaseRunActionException as exception:
            self.logger.exception(exception) # TODO
            
        # after dumping config in another directory, pathes to project files like
        # readme and source files are wrong. We cannot just change the pathes to the new
        # one in project configuration, because they would be outside of the project(in
        # parent directory) and pip doesn't support this. Instead, we create symlinks
        # to all project files in the directory with dumped config.
        # - readme is needed to avoid error that it is missing
        # - source files are needed for runtime environment. During installation of
        #   requirements, pip automatically resolves symlinks and editable pathes point
        #   to original source files, not to temporary symlinks
        #
        # question: filemanager should be used here?
        for item in project_dir_path.iterdir():
            if item.name == 'finecode_config_dump' or item.name == 'pyproject.toml':
                # ignore:
                # - dir with dumped config
                # - dumped config
                continue
            
            new_item_path = dump_dir_path / item.name
            if new_item_path.exists():
                if new_item_path.is_symlink():
                    new_item_path.unlink()
                elif new_item_path.is_dir():
                    shutil.rmtree(new_item_path)
                else:
                    new_item_path.unlink()
            new_item_path.symlink_to(item, target_is_directory=item.is_dir())

        return prepare_envs_action.PrepareEnvsRunResult(results=[]) # TODO
