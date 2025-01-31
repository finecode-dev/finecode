from pathlib import Path

from loguru import logger

from finecode.workspace_manager.context import WorkspaceContext


def find_project_with_action_for_file(
    file_path: Path,
    action_name: str,
    ws_context: WorkspaceContext,
) -> Path:
    """
    NOTE: It can be that file_path belongs to one project, but this project doesn't
          implemented the action we are looking for. In this case case we need to check parent project
          and so on.
    """
    logger.trace(f"Find project with action {action_name} for file {file_path.as_posix()}")

    # first find all projects to which file belongs
    file_projects_pathes: list[Path] = []
    # TODO: save in workspace context to avoid recalculating
    sorted_project_dirs = list(ws_context.ws_projects.keys())
    # reversed sort of pathes sorts them so, that children are always before parents
    sorted_project_dirs.sort(reverse=True)
    for project_dir in sorted_project_dirs:
        if file_path.is_relative_to(project_dir):
            file_projects_pathes.append(project_dir)
        else:
            continue

    if len(file_projects_pathes) == 0:
        raise ValueError(
            f"File {file_path} doesn't belong to one of projects in workspace. Workspace projects: {sorted_project_dirs}"
        )

    dir_path = file_path if file_path.is_dir() else file_path.parent
    dir_path_str = dir_path.as_posix()
    if (
        ws_context.project_path_by_dir_and_action.get(dir_path_str, {}).get(action_name, None)
        is not None
    ):
        logger.trace(
            f"Found in context: {ws_context.project_path_by_dir_and_action[dir_path_str][action_name]}"
        )
        return ws_context.project_path_by_dir_and_action[dir_path_str][action_name]

    if dir_path_str not in ws_context.project_path_by_dir_and_action:
        ws_context.project_path_by_dir_and_action[dir_path_str] = {}

    for project_dir_path in file_projects_pathes:
        project_actions = ws_context.ws_projects[project_dir_path].actions
        try:
            next(action for action in project_actions if action.name == action_name)
        except StopIteration:
            continue

        ws_context.project_path_by_dir_and_action[dir_path_str][action_name] = project_dir_path
        return project_dir_path

    raise ValueError(f"File belongs to project(s), but no of them has action {action_name}: {file_projects_pathes}")


def is_project(dir_path: Path) -> bool:
    pyproject_path = dir_path / "pyproject.toml"
    if pyproject_path.exists():
        return True

    requirements_path = dir_path / "requirements.txt"
    if requirements_path.exists():
        return True

    return False
