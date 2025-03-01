from pathlib import Path
from typing import Any

from loguru import logger

from finecode.workspace_manager import context, domain, find_project
from finecode.workspace_manager.runner import runner_client


class ActionRunFailed(Exception): ...


async def find_action_project_and_run_in_runner(
    file_path: Path,
    action_name: str,
    params: list[Any],
    ws_context: context.WorkspaceContext,
) -> Any | None:
    try:
        project_path = find_project.find_project_with_action_for_file(
            file_path=file_path,
            action_name=action_name,
            ws_context=ws_context,
        )
    except find_project.FileNotInWorkspaceException:
        return None
    except find_project.FileHasNotActionException as error:
        raise error
    except ValueError as error:
        logger.warning(f"Skip {action_name} on {file_path}: {error}")
        raise ActionRunFailed(error)

    project_status = ws_context.ws_projects[project_path].status
    if project_status != domain.ProjectStatus.RUNNING:
        logger.info(
            f"Extension runner {project_path} is not running, "
            f"status: {project_status.name}"
        )
        raise ActionRunFailed(
            f"Extension runner {project_path} is not running, "
            f"status: {project_status.name}"
        )

    runner = ws_context.ws_projects_extension_runners[project_path]
    try:
        response = await runner_client.run_action(
            runner=runner, action_name=action_name, params=params
        )
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error on running action {action_name} on {file_path}: {error}")
        raise ActionRunFailed(error)

    return response
