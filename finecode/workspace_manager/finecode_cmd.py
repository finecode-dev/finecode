from pathlib import Path


def get_finecode_cmd(project_path: Path) -> str:
    sh_path = project_path / "finecode.sh"

    if not sh_path.exists():
        raise ValueError(f"finecode.sh not found in project {project_path}")

    with open(sh_path, "r") as sh_file:
        sh_cmd = sh_file.readline()

    return sh_cmd
