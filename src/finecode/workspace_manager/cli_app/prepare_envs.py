import pathlib
import shutil
from loguru import logger

from finecode.workspace_manager import context, services, domain
from finecode.workspace_manager.config import read_configs, collect_actions
from finecode.workspace_manager.cli_app import run as run_cli
from finecode.workspace_manager.runner import manager as runner_manager


async def prepare_envs(workdir_path: pathlib.Path) -> None:
    # similar to `run_actions`, but with certain differences:
    # - prepare_envs doesn't support presets because `dev_no_runtime` env most
    #   probably doesn't exist yet
    # - we don't need to check missing actions, because prepare_envs is a builtin action
    #   and it exists always
    ws_context = context.WorkspaceContext([workdir_path])
    await read_configs.read_projects_in_dir(
        dir_path=workdir_path, ws_context=ws_context
    )
    
    # `prepare_envs` can be run only from workspace/project root. Validate this
    if workdir_path not in ws_context.ws_projects:
        # TODO: better exception
        raise Exception("prepare_env can be run only from workspace/project root")

    projects = list(ws_context.ws_projects.values())
    
    # Collect actions in relevant projects
    for project in projects:
        await read_configs.read_project_config(project=project, ws_context=ws_context, resolve_presets=False)
        collect_actions.collect_actions(project_path=project.dir_path, ws_context=ws_context)

    actions_by_projects: dict[pathlib.Path, list[str]] = {project.dir_path: ['prepare_envs'] for project in projects}
    # action payload can be kept empty because it will be filled in payload preprocessor
    action_payload: dict[str, str] = {}

    try:
        # try to start runner in 'dev_workspace' env of each project. If venv doesn't
        # exist or doesn't work, recreate it by running actions in the current env.
        await start_or_recreate_all_dev_workspace_envs(projects=projects, ws_context=ws_context)
        
        # now all 'dev_workspace' envs are valid, run 'prepare_envs' in them to create
        # envs in each subproject.
        await run_cli.start_required_environments(actions_by_projects, ws_context)
        
        try:
            await run_cli.run_actions_in_all_projects(
                actions_by_projects, action_payload, ws_context, concurrently=True
            )
        except run_cli.RunFailed as error:
            logger.error(error.message)
    finally:
        services.on_shutdown(ws_context)


async def start_or_recreate_all_dev_workspace_envs(projects: list[domain.Project], ws_context: context.WorkspaceContext) -> None:
    projects_dirs_with_invalid_envs: list[pathlib.Path] = []
    
    for project in projects:
        try:
            runner = await runner_manager.start_runner(
                project_def=project,
                env_name='dev_workspace', 
                ws_context=ws_context
            )
        except runner_manager.RunnerFailedToStart as e:
            logger.warning(f"Failed to start runner for env 'dev_workspace' in project '{project.name}': {e}, recreate it")
            projects_dirs_with_invalid_envs.append(project.dir_path)

    if len(projects_dirs_with_invalid_envs) > 0:
        # to recreate dev_workspace env, run `prepare_envs` in runner of current project
        current_project_dir_path = ws_context.ws_dirs_paths[0]
        current_project = ws_context.ws_projects[current_project_dir_path]
        try:
            runner = await runner_manager.start_runner(project_def=current_project, env_name='dev_workspace', ws_context=ws_context)
        except runner_manager.RunnerFailedToStart as exception:
            # TODO
            raise exception

        envs = []
        for project_dir_path in projects_dirs_with_invalid_envs:
            # dependencies in `dev_workspace` should be simple and installable without
            # dumping
            envs.append({"name": "dev_workspace", "venv_dir_path": project_dir_path / '.venvs' / 'dev_workspace', "project_def_path": project_dir_path / 'pyproject.toml' })
        
        # remove existing invalid envs
        for env_info in envs:
            if env_info.venv_dir_path.exists():
                logger.trace(f"{env_info.venv_dir_path} was invalid, remove it")
                shutil.rmtree(env_info.venv_dir_path)

        try:
            # TODO: check result
            await services.run_action(
                action_name='prepare_dev_workspaces_envs',
                params={ "envs": envs, },
                project_def=current_project,
                ws_context=ws_context,
                result_format=services.RunResultFormat.STRING,
                preprocess_payload=False
            )
        finally:
            runner_manager.stop_extension_runner_sync(runner)
