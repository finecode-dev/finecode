import asyncio
import json
from pathlib import Path
from typing import Any, TypeVar

from loguru import logger
from lsprotocol import types

from finecode.workspace_manager import context, domain, find_project
from finecode.workspace_manager.runner import runner_client, runner_info

ResponseType = TypeVar("ResponseType")


class ActionRunFailed(Exception):
    ...


async def find_action_project_and_run_in_runner(
    file_path: Path,
    action_name: str,
    params: list[Any],
    ws_context: context.WorkspaceContext,
) -> ResponseType:
    try:
        project_path = find_project.find_project_with_action_for_file(
            file_path=file_path,
            action_name=action_name,
            ws_context=ws_context,
        )
    except ValueError as error:
        logger.warning(f"Skip {action_name} on {file_path}: {error}")
        raise ActionRunFailed(error)

    project_status = ws_context.ws_projects[project_path].status
    if project_status != domain.ProjectStatus.RUNNING:
        logger.info(
            f"Extension runner {project_path} is not running, status: {project_status.name}"
        )
        raise ActionRunFailed(f"Extension runner {project_path} is not running, status: {project_status.name}")

    runner = ws_context.ws_projects_extension_runners[project_path]
    try:
        response = await runner_client.send_request(
            runner=runner,
            method=types.WORKSPACE_EXECUTE_COMMAND,
            params=types.ExecuteCommandParams(
                command="actions/run",
                arguments=[
                    action_name,
                    *params,
                ],
            ),
            timeout=None
        )
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error on running action {action_name} on {file_path}: {error}")
        raise ActionRunFailed(error)

    return json.loads(response.result)


async def run_action_in_all_runners(
    action_name: str, params: Any, ws_context: context.WorkspaceContext # , response_type: ResponseType
) -> list[ResponseType]:
    # TODO: find out in which runners it should run
    # running_runners = [
    #     runner
    #     for runner in ws_context.ws_projects_extension_runners.values()
    #     if ws_context.ws_projects[runner.working_dir_path].status == domain.ProjectStatus.RUNNING
    # ]
    
    relevant_runners: list[runner_info.ExtensionRunnerInfo] = []
    for runner in ws_context.ws_projects_extension_runners.values():
        ...

    # TODO: filter out if ws_context.ws_projects[runner.working_dir_path].status != domain.ProjectStatus.RUNNING
    request_coros = [
        runner_client.send_request(runner=runner, method=action_name, params=params)
        for runner in relevant_runners
    ]
    responses = await asyncio.gather(*request_coros, return_exceptions=True)
    filtered_responses: list[ResponseType] = []
    for index, response in enumerate(responses):
        if response is None or not isinstance(response, Exception):
            filtered_responses.append(response)
        else:
            logger.warning(f"Got error: {response} from {running_runners[index].working_dir_path}")
            filtered_responses.append(None)

    return filtered_responses


async def find_project_and_run_in_runner(
    file_path: Path,
    method: str,
    params: Any,
    response_type: ResponseType,
    ws_context: context.WorkspaceContext,
) -> ResponseType:
    try:
        project_path = find_project.find_project_with_action_for_file(
            file_path=file_path,
            action_name=method,
            ws_context=ws_context,
        )
    except ValueError:
        logger.warning(f"Skip {method} on {file_path}")
        return None

    project_status = ws_context.ws_projects[project_path].status
    if project_status != domain.ProjectStatus.RUNNING:
        logger.info(
            f"Extension runner {project_path} is not running, status: {project_status.name}"
        )
        return None

    runner = ws_context.ws_projects_extension_runners[project_path]
    try:
        response = await runner_client.send_request(runner=runner, method=method, params=params)
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error document diagnostic {file_path}: {error}")
        return None

    if not isinstance(response, response_type):
        raise ValueError("Unexpected response type")

    return response


async def run_in_all_runners(
    method: str, params: Any, response_type: ResponseType, ws_context: context.WorkspaceContext
) -> list[ResponseType]:
    running_runners = [
        runner
        for runner in ws_context.ws_projects_extension_runners.values()
        if ws_context.ws_projects[runner.working_dir_path].status == domain.ProjectStatus.RUNNING
    ]
    request_coros = [
        runner_client.send_request(runner=runner, method=method, params=params)
        for runner in running_runners
    ]
    responses = await asyncio.gather(*request_coros, return_exceptions=True)
    filtered_responses: list[ResponseType] = []
    for index, response in enumerate(responses):
        if isinstance(response, response_type):
            filtered_responses.append(response)
        elif response is None:
            filtered_responses.append(response)
        else:
            logger.warning(f"Got error: {response} from {running_runners[index].working_dir_path}")
            filtered_responses.append(None)

    return filtered_responses
