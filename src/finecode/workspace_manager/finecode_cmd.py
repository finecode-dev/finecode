from pathlib import Path


def get_python_cmd(project_path: Path, env_name: str) -> str:
    venv_python_path = project_path / ".venvs" / env_name / "bin" / "python"

    if not venv_python_path.exists():
        raise ValueError(f"{env_name} venv not found in project {project_path}")

    return venv_python_path.as_posix()
