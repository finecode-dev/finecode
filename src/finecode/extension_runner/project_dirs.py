import hashlib
import os
from pathlib import Path

from finecode.extension_runner import app_dirs


def get_project_dir(project_path: Path) -> Path:
    root_cache_dir = Path(app_dirs.get_app_dirs().user_cache_dir)
    projects_dir = root_cache_dir / "projects"

    m = hashlib.md5(usedforsecurity=False)
    m.update(project_path.as_posix().encode())
    project_path_hash = m.hexdigest()[:8]

    project_dir_name = f"{project_path.name}-{project_path_hash}"
    project_dir = projects_dir / project_dir_name
    os.makedirs(project_dir, exist_ok=True)
    return project_dir
