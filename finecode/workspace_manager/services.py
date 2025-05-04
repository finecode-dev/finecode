import pathlib
import typing

from loguru import logger

from finecode.workspace_manager import (
    context,
    domain,
    payload_preprocessor,
    user_messages,
)
from finecode.workspace_manager.runner import manager as runner_manager
from finecode.workspace_manager.runner import runner_client


async def restart_extension_runner(
    runner_working_dir_path: pathlib.Path, ws_context: context.WorkspaceContext
) -> None:
    # TODO: reload config?
    try:
        runner = ws_context.ws_projects_extension_runners[runner_working_dir_path]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    await runner_manager.stop_extension_runner(runner)
    del ws_context.ws_projects_extension_runners[runner_working_dir_path]

    new_runner = await runner_manager.start_extension_runner(
        runner_dir=runner_working_dir_path, ws_context=ws_context
    )
    if new_runner is None:
        logger.error("Extension runner didn't start")
        return

    ws_context.ws_projects_extension_runners[runner_working_dir_path] = new_runner
    await runner_manager._init_runner(
        new_runner,
        ws_context.ws_projects[runner.working_dir_path],
        ws_context,
    )


def on_shutdown(ws_context: context.WorkspaceContext):
    running_runners = [
        runner
        for runner in ws_context.ws_projects_extension_runners.values()
        if ws_context.ws_projects[runner.working_dir_path].status
        == domain.ProjectStatus.RUNNING
    ]
    logger.trace(f"Stop all {len(running_runners)} running extension runners")

    for runner in running_runners:
        runner_manager.stop_extension_runner_sync(runner=runner)


RunResultFormat = runner_client.RunResultFormat
RunActionResponse = runner_client.RunActionResponse


async def run_action(
    action_name: str,
    params: dict[str, typing.Any],
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    result_format: RunResultFormat = RunResultFormat.JSON,
) -> RunActionResponse:
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action_name} with {formatted_params}")

    if project_def.status != domain.ProjectStatus.RUNNING:
        logger.error(
            f"Extension runner is not running in {project_def.dir_path}."
            " Please check logs."
        )
        return RunActionResponse(result={}, return_code=1)

    payload = payload_preprocessor.preprocess_for_project(
        action_name=action_name, payload=params, project_dir_path=project_def.dir_path
    )

    # extension runner is running for this project, send command to it
    try:
        response = await runner_client.run_action(
            runner=ws_context.ws_projects_extension_runners[project_def.dir_path],
            action_name=action_name,
            params=payload,
            options={"result_format": result_format},
        )
    except runner_client.BaseRunnerRequestException as error:
        await user_messages.error(f"Action {action_name} failed: {error.message}")
        if result_format == runner_client.RunResultFormat.JSON:
            return RunActionResponse(result={}, return_code=1)
        else:
            return RunActionResponse(result="", return_code=1)

    return response
