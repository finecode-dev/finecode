import os
import pathlib

from loguru import logger

from finecode import context, services
from finecode.config import config_models, read_configs
from finecode.runner import manager as runner_manager


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

    # read configs without presets, this is required to be able to start runners in
    # the next step
    for project in ws_context.ws_projects.values():
        try:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=False
            )
        except config_models.ConfigurationError as exception:
            raise DumpFailed(
                f"Reading project configs(without presets) in {project.dir_path} failed: {exception.message}"
            )

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
        dump_dir_path = project_dir_path / "finecode_config_dump"
        dump_file_path = dump_dir_path / "pyproject.toml"
        project_raw_config = ws_context.ws_projects_raw_configs[project_dir_path]
        project_def = ws_context.ws_projects[project_dir_path]

        await services.run_action(
            action_name="dump_config",
            params={
                "source_file_path": project_def.def_path,
                "project_raw_config": project_raw_config,
                "target_file_path": dump_file_path,
            },
            project_def=project_def,
            ws_context=ws_context,
            result_format=services.RunResultFormat.STRING,
            preprocess_payload=False,
        )
        logger.info(f"Dumped config into {dump_file_path}")
    finally:
        services.on_shutdown(ws_context)
