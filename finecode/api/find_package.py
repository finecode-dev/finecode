from pathlib import Path

from loguru import logger

from .collect_actions import _collect_actions_in_config
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
    workspace_path: Path,
    ws_context: WorkspaceContext,
) -> Path:
    logger.trace(
        f"Find package with action {action_name} for file {file_path.as_posix()}"
    )
    try:
        file_path.relative_to(workspace_path)
    except ValueError:
        raise ValueError(
            f"File path {file_path} is not inside of workspace {workspace_path}"
        )

    dir_path = (
        file_path.as_posix() if file_path.is_dir() else file_path.parent.as_posix()
    )
    if (
        ws_context.package_path_by_dir_and_action.get(dir_path, {}).get(
            action_name, None
        )
        is not None
    ):
        logger.trace(
            f"Found in context: {ws_context.package_path_by_dir_and_action[dir_path][action_name]}"
        )
        return ws_context.package_path_by_dir_and_action[dir_path][action_name]

    if dir_path not in ws_context.package_path_by_dir_and_action:
        ws_context.package_path_by_dir_and_action[dir_path] = {}
    current_path = file_path
    while current_path != workspace_path:
        pyproject_path = current_path / "pyproject.toml"
        if pyproject_path.exists() and _finecode_is_enabled_in_def(
            def_file=pyproject_path
        ):
            _, all_actions = _collect_actions_in_config(
                pyproject_path=pyproject_path, ws_context=ws_context
            )
            if action_name in all_actions:
                ws_context.package_path_by_dir_and_action[dir_path][
                    action_name
                ] = current_path
                return current_path
        current_path = current_path.parent

    ws_context.package_path_by_dir_and_action[dir_path][action_name] = workspace_path
    return workspace_path
