import os
import pathlib

from loguru import logger

from finecode.workspace_manager import context, services
from finecode.workspace_manager.config import read_configs, dump_configs
from finecode.workspace_manager.runner import manager as runner_manager


class DumpFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def dump_config(workdir_path: pathlib.Path, project_name: str):
    ws_context = context.WorkspaceContext([workdir_path])
    # it could be optimized by looking for concrete project instead of all
    await read_configs.read_projects_in_dir(
        dir_path=workdir_path, ws_context=ws_context
    )

    # project is provided. Filter out other projects if there are more, they would
    # not be used (run can be started in a workspace with also other projects)
    ws_context.ws_projects = {
        project_dir_path: project
        for project_dir_path, project in ws_context.ws_projects.items()
        if project.name == project_name
    }
    
    # start runner to init project config
    try:
        try:
            await runner_manager.update_runners(ws_context)
        except runner_manager.RunnerFailedToStart as exception:
            raise DumpFailed(
                f"One or more projects are misconfigured, runners for them didn't"
                f" start: {exception.message}. Check logs for details."
            )

        # Some tools like IDE extensions for syntax highlighting rely on
        # file name. Keep file name of config the same and save in subdirectory
        project_dir_path = list(ws_context.ws_projects.keys())[0]
        dump_dir_path = project_dir_path / 'finecode_config_dump'
        dump_file_path = dump_dir_path / 'pyproject.toml'
        
        project_raw_config = ws_context.ws_projects_raw_configs[project_dir_path]
        raw_config_str = dump_configs.dump_config(project_raw_config)

        os.makedirs(dump_dir_path, exist_ok=True)
        with open(dump_file_path, 'w') as dump_file:
            dump_file.write(raw_config_str)
        
        logger.info(f"Dumped config into {dump_file_path}")
    finally:
        services.on_shutdown(ws_context)
