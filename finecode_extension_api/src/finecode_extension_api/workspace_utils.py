import pathlib


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
        for project in sorted_projects:
            if file.is_relative_to(project):
                result.setdefault(project, []).append(file)
                break
    return result
