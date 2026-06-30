import pathlib

from loguru import logger


def group_files_by_project(
    files: list[pathlib.Path],
    project_paths: list[pathlib.Path],
) -> dict[pathlib.Path, list[pathlib.Path]]:
    """Group files by their owning project.

    Each file is assigned to the project whose root is the deepest (longest)
    ancestor of the file path. Files not under any project are excluded.
    """
    sorted_projects = sorted(project_paths, key=lambda p: len(p.parts), reverse=True)
    result: dict[pathlib.Path, list[pathlib.Path]] = {}
    for file in files:
        exists_on_disk = file.exists()
        matched = False
        for project in sorted_projects:
            if file.is_relative_to(project):
                logger.debug(
                    f"group_files_by_project: assigned {file} to project {project}"
                    f" (exists_on_disk={exists_on_disk})"
                )
                result.setdefault(project, []).append(file)
                matched = True
                break
        if not matched:
            logger.debug(
                f"group_files_by_project: {file} not under any known project"
                f" (exists_on_disk={exists_on_disk})"
            )
    return result
