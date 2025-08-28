import pathlib
import shutil

from loguru import logger

from finecode import context, domain, proxy_utils, services
from finecode.cli_app import run as run_cli
from finecode.config import collect_actions, config_models, read_configs
from finecode.runner import manager as runner_manager


class PrepareEnvsFailed(Exception): ...


async def prepare_envs(workdir_path: pathlib.Path, recreate: bool) -> None:
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
        raise PrepareEnvsFailed(
            "prepare_env can be run only from workspace/project root"
        )

    invalid_projects = [
        project
        for project in ws_context.ws_projects.values()
        if project.status == domain.ProjectStatus.CONFIG_INVALID
    ]
    if len(invalid_projects) > 0:
        raise PrepareEnvsFailed(
            f"Projects have invalid configuration: {invalid_projects}"
        )

    # prepare envs only in projects with valid configurations and which use finecode
    projects = [
        project
        for project in ws_context.ws_projects.values()
        if project.status == domain.ProjectStatus.CONFIG_VALID
    ]

    # Collect actions in relevant projects
    for project in projects:
        try:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=False
            )
            collect_actions.collect_actions(
                project_path=project.dir_path, ws_context=ws_context
            )
        except config_models.ConfigurationError as exception:
            raise PrepareEnvsFailed(
                f"Reading project config and collecting actions in {project.dir_path} failed: {exception.message}"
            )

    try:
        # try to start runner in 'dev_workspace' env of each project. If venv doesn't
        # exist or doesn't work, recreate it by running actions in the current env.
        if recreate:
            remove_dev_workspace_envs(projects=projects, workdir_path=workdir_path)

        await check_or_recreate_all_dev_workspace_envs(
            projects=projects,
            workdir_path=workdir_path,
            recreate=recreate,
            ws_context=ws_context,
        )

        # now all 'dev_workspace' envs are valid, run 'prepare_runners' in them to create
        # venvs and install runners and presets in them. This is required to be able to
        # resolve presets which can contain additional dependency configurations.
        # Only then run `prepare_envs` to install dependencies in each subproject.

        actions_by_projects: dict[pathlib.Path, list[str]] = {
            project.dir_path: ["prepare_runners"] for project in projects
        }
        # action payload can be kept empty because it will be filled in payload preprocessor
        action_payload: dict[str, str | bool] = {"recreate": recreate}

        try:
            await proxy_utils.start_required_environments(
                actions_by_projects, ws_context
            )
        except proxy_utils.StartingEnvironmentsFailed as exception:
            raise PrepareEnvsFailed(
                f"Failed to start environments for running 'prepare_runners': {exception.message}"
            )

        try:
            (result_output, result_return_code) = (
                await run_cli.run_actions_in_all_projects(
                    actions_by_projects, action_payload, ws_context, concurrently=True
                )
            )
        except run_cli.RunFailed as error:
            logger.error(error.message)
            result_output = error.message
            result_return_code = 1

        if result_return_code != 0:
            raise PrepareEnvsFailed(result_output)

        # reread projects configs, now with resolved presets
        # to be able to resolve presets, start dev_no_runtime runners first
        try:
            await runner_manager.start_runners_with_presets(
                projects=projects, ws_context=ws_context
            )
        except runner_manager.RunnerFailedToStart as exception:
            raise PrepareEnvsFailed(
                f"Starting runners with presets failed: {exception.message}"
            )

        for project in projects:
            try:
                await read_configs.read_project_config(
                    project=project, ws_context=ws_context
                )
                collect_actions.collect_actions(
                    project_path=project.dir_path, ws_context=ws_context
                )
            except config_models.ConfigurationError as exception:
                raise PrepareEnvsFailed(
                    f"Rereading project config with presets and collecting actions in {project.dir_path} failed: {exception.message}"
                )

        actions_by_projects: dict[pathlib.Path, list[str]] = {
            project.dir_path: ["prepare_envs"] for project in projects
        }
        # action payload can be kept empty because it will be filled in payload preprocessor
        action_payload: dict[str, str | bool] = {"recreate": recreate}

        try:
            (result_output, result_return_code) = (
                await run_cli.run_actions_in_all_projects(
                    actions_by_projects, action_payload, ws_context, concurrently=True
                )
            )
        except run_cli.RunFailed as error:
            logger.error(error.message)
            result_output = error.message
            result_return_code = 1

        if result_return_code != 0:
            raise PrepareEnvsFailed(result_output)
    finally:
        services.on_shutdown(ws_context)


def remove_dev_workspace_envs(
    projects: list[domain.Project], workdir_path: pathlib.Path
) -> None:
    for project in projects:
        if project.dir_path == workdir_path:
            # skip removing `dev_workspace` env of the current project, because user
            # is responsible for keeping it correct
            continue

        runner_manager.remove_runner_venv(
            runner_dir=project.dir_path, env_name="dev_workspace"
        )


async def check_or_recreate_all_dev_workspace_envs(
    projects: list[domain.Project],
    workdir_path: pathlib.Path,
    recreate: bool,
    ws_context: context.WorkspaceContext,
) -> None:
    # NOTE: this function can start new extensions runner, don't forget to call
    #       on_shutdown if you use it
    projects_dirs_with_valid_envs: list[pathlib.Path] = []
    projects_dirs_with_invalid_envs: list[pathlib.Path] = []

    for project in projects:
        if project.dir_path == workdir_path:
            # skip checking `dev_workspace` env of the current project, because user
            # is responsible for keeping it correct
            continue

        runner_is_valid = await runner_manager.check_runner(
            runner_dir=project.dir_path, env_name="dev_workspace"
        )
        if runner_is_valid:
            projects_dirs_with_valid_envs.append(project.dir_path)
        else:
            if recreate:
                logger.trace(
                    f"Recreate runner for env 'dev_workspace' in project '{project.name}'"
                )
            else:
                logger.warning(
                    f"Runner for env 'dev_workspace' in project '{project.name}' is invalid, recreate it"
                )
            projects_dirs_with_invalid_envs.append(project.dir_path)

    # to recreate dev_workspace env, run `prepare_envs` in runner of current project
    current_project_dir_path = ws_context.ws_dirs_paths[0]
    current_project = ws_context.ws_projects[current_project_dir_path]
    try:
        runner = await runner_manager.start_runner(
            project_def=current_project, env_name="dev_workspace", ws_context=ws_context
        )
    except runner_manager.RunnerFailedToStart as exception:
        # TODO
        raise exception

    envs = []

    # run pip install in dev_workspace even if env exists to make sure that correct
    # dependencies are installed
    for project_dir_path in projects_dirs_with_valid_envs:
        if project_dir_path == workdir_path:
            # skip installation of dependencies in `dev_workspace` env of the
            # current project, because user is responsible for keeping them
            # up-to-date
            continue

        # dependencies in `dev_workspace` should be simple and installable without
        # dumping
        envs.append(
            {
                "name": "dev_workspace",
                "venv_dir_path": project_dir_path / ".venvs" / "dev_workspace",
                "project_def_path": project_dir_path / "pyproject.toml",
            }
        )

    if len(projects_dirs_with_invalid_envs) > 0:
        invalid_envs = []

        for project_dir_path in projects_dirs_with_invalid_envs:
            # dependencies in `dev_workspace` should be simple and installable without
            # dumping
            invalid_envs.append(
                {
                    "name": "dev_workspace",
                    "venv_dir_path": project_dir_path / ".venvs" / "dev_workspace",
                    "project_def_path": project_dir_path / "pyproject.toml",
                }
            )

        # remove existing invalid envs
        for env_info in invalid_envs:
            if env_info["venv_dir_path"].exists():
                logger.trace(f"{env_info['venv_dir_path']} was invalid, remove it")
                shutil.rmtree(env_info["venv_dir_path"])

        envs += invalid_envs

    try:
        action_result = await services.run_action(
            action_name="prepare_dev_workspaces_envs",
            params={
                "envs": envs,
            },
            project_def=current_project,
            ws_context=ws_context,
            result_format=services.RunResultFormat.STRING,
            preprocess_payload=False,
        )
    except services.ActionRunFailed as exception:
        raise PrepareEnvsFailed(
            f"'prepare_dev_workspaces_env' failed in {current_project.name}: {exception.message}"
        )

    if action_result.return_code != 0:
        raise PrepareEnvsFailed(
            f"'prepare_dev_workspaces_env' ended in {current_project.name} with return code {action_result.return_code}: {action_result.result}"
        )
