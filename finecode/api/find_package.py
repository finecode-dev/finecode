from pathlib import Path

from loguru import logger

from .collect_actions import collect_actions
from ._read_configs import _finecode_is_enabled_in_def
from finecode.workspace_context import WorkspaceContext


def find_package_for_file(file_path: Path, workspace_path: Path) -> Path:
    try:
        file_path.relative_to(workspace_path)
    except ValueError:
        raise ValueError(
            f"File path {file_path} is not inside of workspace {workspace_path}"
        )

    current_path = file_path
    while current_path != workspace_path:
        pyproject_path = current_path / "pyproject.toml"
        if pyproject_path.exists() and _finecode_is_enabled_in_def(
            def_file=pyproject_path
        ):
            return current_path
        current_path = current_path.parent

    return workspace_path


def find_package_with_action_for_file(
    file_path: Path,
    action_name: str,
    ws_context: WorkspaceContext,
) -> Path:
    logger.trace(
        f"Find package with action {action_name} for file {file_path.as_posix()}"
    )

    workspace_path: Path | None = None
    for ws_dir in ws_context.ws_dirs_paths:
        try:
            file_path.relative_to(ws_dir)
            workspace_path = ws_dir
            break
        except ValueError:
            ...

    if workspace_path is None:
        raise ValueError("File doesn't belong to the workspace")

    dir_path = file_path if file_path.is_dir() else file_path.parent
    dir_path_str = (
        file_path.as_posix() if file_path.is_dir() else file_path.parent.as_posix()
    )
    if (
        ws_context.package_path_by_dir_and_action.get(dir_path_str, {}).get(
            action_name, None
        )
        is not None
    ):
        logger.trace(
            f"Found in context: {ws_context.package_path_by_dir_and_action[dir_path_str][action_name]}"
        )
        return ws_context.package_path_by_dir_and_action[dir_path_str][action_name]

    if dir_path_str not in ws_context.package_path_by_dir_and_action:
        ws_context.package_path_by_dir_and_action[dir_path_str] = {}
    current_path = dir_path
    while current_path != workspace_path:
        pyproject_path = current_path / "pyproject.toml"
        if pyproject_path.exists() and _finecode_is_enabled_in_def(
            def_file=pyproject_path
        ):
            actions = collect_actions(package_path=current_path, ws_context=ws_context)
            try:
                next(action for action in actions if action.name == action_name)
                ws_context.package_path_by_dir_and_action[dir_path_str][
                    action_name
                ] = current_path
                return current_path
            except StopIteration:
                ...
        current_path = current_path.parent

    ws_context.package_path_by_dir_and_action[dir_path_str][action_name] = workspace_path
    return workspace_path


def is_package(dir_path: Path) -> bool:
    pyproject_path = dir_path / 'pyproject.toml'
    if pyproject_path.exists():
        return True
    
    requirements_path = dir_path / 'requirements.txt'
    if requirements_path.exists():
        return True
    
    return False
