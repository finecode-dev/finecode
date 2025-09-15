from pathlib import Path

from finecode import context
from finecode.services import run_service


class FailedToGetProjectFiles(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def get_files_by_projects(
    projects_dirs_paths: list[Path], ws_context: context.WorkspaceContext
) -> dict[Path, list[Path]]:
    files_by_project_dir: dict[Path, list[Path]] = {}
    actions_by_project = {
        project_dir_path: ["list_project_files_by_lang"]
        for project_dir_path in projects_dirs_paths
    }
    action_payload = {}

    try:
        results_by_project = await run_service.run_actions_in_projects(
            actions_by_project=actions_by_project,
            action_payload=action_payload,
            ws_context=ws_context,
            concurrently=False,
            result_format=run_service.RunResultFormat.JSON,
        )
    except run_service.ActionRunFailed as exception:
        # TODO: handle it overall
        raise FailedToGetProjectFiles(exception.message)

    for project_dir_path, action_results in results_by_project.items():
        list_project_files_action_result = action_results["list_project_files_by_lang"]
        if list_project_files_action_result.return_code != 0:
            raise FailedToGetProjectFiles(
                f"'list_project_files_by_lang' action ended in {project_dir_path} with return code {list_project_files_action_result.return_code}: {list_project_files_action_result.result}"
            )
        project_files_by_lang = list_project_files_action_result.result
        files_by_project_dir[project_dir_path] = [
            Path(file_path)
            for file_path in project_files_by_lang["files_by_lang"].get("python", [])
        ]

    return files_by_project_dir


async def get_project_files(
    project_dir_path: Path, ws_context: context.WorkspaceContext
) -> list[Path]:
    files_by_projects = await get_files_by_projects(
        [project_dir_path], ws_context=ws_context
    )
    return files_by_projects[project_dir_path]
