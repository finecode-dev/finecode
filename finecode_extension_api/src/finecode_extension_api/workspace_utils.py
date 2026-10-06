import collections.abc
import os
import pathlib

from loguru import logger


def nested_project_dirs(
    project_path: pathlib.Path,
    workspace_project_paths: collections.abc.Iterable[pathlib.Path],
) -> list[pathlib.Path]:
    """The projects rooted strictly inside *project_path*.

    Their files are theirs, not this project's: they list them themselves and run them
    through their own configuration.  A handler walking this project's directories has
    to stop at these roots, or an operation restricted to the outer project silently
    pulls the inner ones in too, and an unrestricted workspace operation processes those
    files twice.

    *workspace_project_paths* should be the projects that can run actions themselves
    (``actionable_project_paths``).  A nested directory that merely looks like a package
    but has no FineCode configuration has no one else to process its files, so it stays
    the responsibility of the enclosing project.

    Having FineCode configuration is the whole criterion: a configured project whose own
    preset lists none of these files is not thereby handing them back: its files are
    unlisted because of how *it* is configured, and that is a gap to fix in that project,
    not work for the enclosing one to pick up.
    """
    return [
        p
        for p in workspace_project_paths
        if p != project_path and p.is_relative_to(project_path)
    ]


def walk_project_files(
    dir_path: pathlib.Path,
    suffix: str,
    excluded_dirs: collections.abc.Iterable[pathlib.Path] = (),
    *,
    skip_hidden: bool = True,
) -> list[pathlib.Path]:
    """Files under *dir_path* ending in *suffix*, without descending into what is excluded.

    Pruning during the walk rather than filtering its results afterwards.

    Hidden directories (``.venvs``, ``.git``, tool caches) and hidden files (``.ruff.toml``
    and other dotfile configuration) are skipped by default: neither is the project's
    source, and a listing that includes them has ``format`` rewriting the very files that
    configure the tools.
    """
    excluded = {pathlib.Path(p) for p in excluded_dirs}
    files: list[pathlib.Path] = []
    for raw_dir, subdir_names, file_names in os.walk(dir_path):
        current_dir = pathlib.Path(raw_dir)
        # in-place, because os.walk reads this list back to decide where to descend
        subdir_names[:] = [
            name
            for name in subdir_names
            if not (skip_hidden and name.startswith("."))
            and current_dir / name not in excluded
        ]
        files += [
            current_dir / name
            for name in file_names
            if name.endswith(suffix) and not (skip_hidden and name.startswith("."))
        ]
    return files


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
