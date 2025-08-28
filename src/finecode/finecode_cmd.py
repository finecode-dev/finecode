from pathlib import Path


def get_venv_dir_path(project_path: Path, env_name: str) -> Path:
    venv_dir_path = project_path / ".venvs" / env_name
    return venv_dir_path


def get_python_cmd(project_path: Path, env_name: str) -> str:
    venv_dir_path = get_venv_dir_path(project_path=project_path, env_name=env_name)
    venv_python_path = venv_dir_path / "bin" / "python"

    if not venv_python_path.exists():
        raise ValueError(f"{env_name} venv not found in project {project_path}")

    return venv_python_path.as_posix()
