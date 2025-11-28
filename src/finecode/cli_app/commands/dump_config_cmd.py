import pathlib

from loguru import logger

from finecode import context
from finecode.services import run_service, shutdown_service
from finecode.config import config_models, read_configs
from finecode.runner import runner_manager


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

    # Some tools like IDE extensions for syntax highlighting rely on
    # file name. Keep file name of config the same and save in subdirectory
    project_dir_path = list(ws_context.ws_projects.keys())[0]
    dump_dir_path = project_dir_path / "finecode_config_dump"
    dump_file_path = dump_dir_path / "pyproject.toml"
    project_def = ws_context.ws_projects[project_dir_path]
    actions_by_projects = {project_dir_path: ["dump_config"]}

    # start runner to init project config
    try:
        # reread projects configs, now with resolved presets
        # to be able to resolve presets, start runners with presets first
        try:
            await runner_manager.start_runners_with_presets(
                projects=[project_def], ws_context=ws_context
            )
        except runner_manager.RunnerFailedToStart as exception:
            raise DumpFailed(
                f"Starting runners with presets failed: {exception.message}"
            )

        try:
            await run_service.start_required_environments(
                actions_by_projects, ws_context
            )
        except run_service.StartingEnvironmentsFailed as exception:
            raise DumpFailed(
                f"Failed to start environments for running 'dump_config': {exception.message}"
            )

        project_raw_config = ws_context.ws_projects_raw_configs[project_dir_path]

        await run_service.run_action(
            action_name="dump_config",
            params={
                "source_file_path": project_def.def_path,
                "project_raw_config": project_raw_config,
                "target_file_path": dump_file_path,
            },
            project_def=project_def,
            ws_context=ws_context,
            result_format=run_service.RunResultFormat.STRING,
            preprocess_payload=False,
        )
        logger.info(f"Dumped config into {dump_file_path}")
    finally:
        shutdown_service.on_shutdown(ws_context)
