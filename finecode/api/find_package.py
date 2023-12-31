from pathlib import Path


def find_package_for_file(file_path: Path, workspace_path: Path) -> Path:
    try:
        file_path.relative_to(workspace_path)
    except ValueError:
        raise ValueError(f"File path {file_path} is not inside of workspace {workspace_path}")
    
    current_path = file_path
    while current_path != workspace_path:
        pyproject_path = current_path / 'pyproject.toml'
        if pyproject_path.exists():
            # TODO: check if finecode is activated
            return current_path
        current_path = current_path.parent
    
    return workspace_path
