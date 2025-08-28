import pathlib
import typing

import ordered_set
from loguru import logger

from finecode import context, domain, payload_preprocessor, user_messages
from finecode.runner import manager as runner_manager
from finecode.runner import runner_client, runner_info


async def restart_extension_runners(
    runner_working_dir_path: pathlib.Path, ws_context: context.WorkspaceContext
) -> None:
    # TODO: reload config?
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    new_runners_by_env: dict[str, runner_info.ExtensionRunnerInfo] = {}
    for runner in runners_by_env.values():
        await runner_manager.stop_extension_runner(runner)

        new_runner = await runner_manager.start_extension_runner(
            runner_dir=runner_working_dir_path,
            env_name=runner.env_name,
            ws_context=ws_context,
        )
        if new_runner is None:
            logger.error("Extension runner didn't start")
            continue
        new_runners_by_env[runner.env_name] = new_runner

    ws_context.ws_projects_extension_runners[runner_working_dir_path] = (
        new_runners_by_env
    )

    # parallel?
    for runner in new_runners_by_env.values():
        await runner_manager._init_runner(
            runner,
            ws_context.ws_projects[runner.working_dir_path],
            ws_context,
        )


def on_shutdown(ws_context: context.WorkspaceContext):

    running_runners = []
    for runners_by_env in ws_context.ws_projects_extension_runners.values():
        for runner in runners_by_env.values():
            if runner.status == runner_info.RunnerStatus.RUNNING:
                running_runners.append(runner)

    logger.trace(f"Stop all {len(running_runners)} running extension runners")

    for runner in running_runners:
        runner_manager.stop_extension_runner_sync(runner=runner)

    # TODO: stop MCP if running


class ActionRunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


RunResultFormat = runner_client.RunResultFormat
RunActionResponse = runner_client.RunActionResponse


async def run_action(
    action_name: str,
    params: dict[str, typing.Any],
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    result_format: RunResultFormat = RunResultFormat.JSON,
    preprocess_payload: bool = True,
) -> RunActionResponse:
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action_name} with {formatted_params}")

    if project_def.status != domain.ProjectStatus.CONFIG_VALID:
        raise ActionRunFailed(
            f"Project {project_def.dir_path} has no valid configuration and finecode."
            " Please check logs."
        )

    if preprocess_payload:
        payload = payload_preprocessor.preprocess_for_project(
            action_name=action_name,
            payload=params,
            project_dir_path=project_def.dir_path,
            ws_context=ws_context,
        )
    else:
        payload = params

    # cases:
    # - base: all action handlers are in one env
    #   -> send `run_action` request to runner in env and let it handle concurrency etc.
    #      It could be done also in workspace manager, but handlers share run context
    # - mixed envs: action handlers are in different envs
    # -- concurrent execution of handlers
    # -- sequential execution of handlers
    assert project_def.actions is not None
    action = next(
        action for action in project_def.actions if action.name == action_name
    )
    all_handlers_envs = ordered_set.OrderedSet(
        [handler.env for handler in action.handlers]
    )
    all_handlers_are_in_one_env = len(all_handlers_envs) == 1

    if all_handlers_are_in_one_env:
        env_name = all_handlers_envs[0]
        response = await _run_action_in_env_runner(
            action_name=action_name,
            payload=payload,
            env_name=env_name,
            project_def=project_def,
            ws_context=ws_context,
            result_format=result_format,
        )
    else:
        # TODO: concurrent vs sequential, this value should be taken from action config
        run_concurrently = False  # action_name == 'lint'
        if run_concurrently:
            ...
            raise NotImplementedError()
        else:
            for handler in action.handlers:
                # TODO: manage run context
                response = await _run_action_in_env_runner(
                    action_name=action_name,
                    payload=payload,
                    env_name=handler.env,
                    project_def=project_def,
                    ws_context=ws_context,
                    result_format=result_format,
                )

    return response


async def _run_action_in_env_runner(
    action_name: str,
    payload: dict[str, typing.Any],
    env_name: str,
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    result_format: RunResultFormat = RunResultFormat.JSON,
):
    runners_by_env = ws_context.ws_projects_extension_runners[project_def.dir_path]
    runner = runners_by_env[env_name]
    if runner.status != runner_info.RunnerStatus.RUNNING:
        raise ActionRunFailed(
            f"Runner {env_name} in project {project_def.dir_path} is not running. Status: {runner.status}"
        )

    try:
        response = await runner_client.run_action(
            runner=runner,
            action_name=action_name,
            params=payload,
            options={"result_format": result_format},
        )
    except runner_client.BaseRunnerRequestException as error:
        runner_log_path = (
            runner.working_dir_path / ".venvs" / runner.env_name / "logs" / "runner.log"
        )
        await user_messages.error(
            f"Action {action_name} failed in {runner.env_name} of {runner.working_dir_path}: {error.message} . Log file: {runner_log_path}"
        )
        raise ActionRunFailed(
            f"Action {action_name} failed in {runner.env_name} of {runner.working_dir_path}: {error.message} . Log file: {runner_log_path}"
        )

    return response
