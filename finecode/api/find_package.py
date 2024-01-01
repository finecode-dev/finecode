from pathlib import Path
from .collect_actions import finecode_is_enabled_in_def, collect_actions_pyproject


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
        if pyproject_path.exists() and finecode_is_enabled_in_def(
            def_file=pyproject_path
        ):
            return current_path
        current_path = current_path.parent

    return workspace_path


def find_package_with_action_for_file(
    file_path: Path, action_name: str, workspace_path: Path
) -> Path:
    try:
        file_path.relative_to(workspace_path)
    except ValueError:
        raise ValueError(
            f"File path {file_path} is not inside of workspace {workspace_path}"
        )

    current_path = file_path
    while current_path != workspace_path:
        pyproject_path = current_path / "pyproject.toml"
        if pyproject_path.exists() and finecode_is_enabled_in_def(
            def_file=pyproject_path
        ):
            _, all_actions = collect_actions_pyproject(pyproject_path=pyproject_path)
            if action_name in all_actions:
                return current_path
        current_path = current_path.parent

    return workspace_path
