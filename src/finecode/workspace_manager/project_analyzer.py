from pathlib import Path


def get_files_by_projects(projects_dirs_paths: list[Path]) -> dict[Path, list[Path]]:
    files_by_projects_dirs: dict[Path, list[Path]] = {}

    if len(projects_dirs_paths) == 1:
        project_dir = projects_dirs_paths[0]
        files_by_projects_dirs[project_dir] = [
            path for path in project_dir.rglob("*.py")
        ]
    else:
        # copy to avoid modifying of argument values
        projects_dirs = projects_dirs_paths.copy()
        # sort by depth so that child items are first
        # default reverse path sorting works so, that child items are before their
        # parents
        projects_dirs.sort(reverse=True)
        for index, project_dir_path in enumerate(projects_dirs):
            files_by_projects_dirs[project_dir_path] = []

            child_project_by_rel_path: dict[Path, Path] = {}
            # find children
            for current_project_dir_path in projects_dirs[:index]:
                if not current_project_dir_path.is_relative_to(project_dir_path):
                    break
                else:
                    rel_to_project_dir_path = current_project_dir_path.relative_to(
                        project_dir_path
                    )
                    child_project_by_rel_path[rel_to_project_dir_path] = (
                        current_project_dir_path
                    )

            # convert child_project_by_rel_path to tree to be able to check whether
            # directory contains subrojects without reiterating
            child_project_tree: dict[str, str] = {}
            for child_rel_path in child_project_by_rel_path.keys():
                current_tree_branch = child_project_tree
                for part in child_rel_path.parts:
                    if part not in current_tree_branch:
                        current_tree_branch[part] = {}
                    current_tree_branch = current_tree_branch[part]

            # use set, because one dir item can have multiple subprojects and we need
            # it only once
            dir_items_with_children: set[str] = set(
                [
                    dir_item_path.parts[0]
                    for dir_item_path in child_project_by_rel_path.keys()
                ]
            )
            if len(dir_items_with_children) == 0:
                # if there are no children with subprojects, we can just rglob
                files_by_projects_dirs[project_dir_path].extend(
                    path for path in project_dir_path.rglob("*.py")
                )
            else:
                # process all dir items which don't have child projects
                for dir_item in project_dir_path.iterdir():
                    if dir_item.name in dir_items_with_children:
                        continue
                    else:
                        if dir_item.suffix == ".py":
                            files_by_projects_dirs[project_dir_path].append(dir_item)
                        elif dir_item.is_dir():
                            files_by_projects_dirs[project_dir_path].extend(
                                path for path in dir_item.rglob("*.py")
                            )

                # process all dir items which have child projects
                #
                # avoid repeating processing of the same directories which would cause
                # duplicates in list of files by saving processed branches
                processed_branches: list[dict[str, str]] = []
                for rel_path in child_project_by_rel_path.keys():
                    rel_path_parts = rel_path.parts
                    current_tree_branch = child_project_tree
                    if current_tree_branch in processed_branches:
                        continue
                    processed_branches.append(current_tree_branch)
                    # iterate from second item because the first one is directory we
                    # currently processing
                    for index in range(len(rel_path_parts[1:])):
                        current_path = project_dir_path / "/".join(
                            rel_path_parts[: index + 1]
                        )
                        current_tree_branch = current_tree_branch[rel_path_parts[index]]
                        if current_tree_branch in processed_branches:
                            continue
                        processed_branches.append(current_tree_branch)

                        for dir_item in current_path.iterdir():
                            if dir_item.suffix == ".py":
                                files_by_projects_dirs[project_dir_path].append(
                                    dir_item
                                )
                            elif dir_item.is_dir():
                                if dir_item.name in current_tree_branch:
                                    # it's a path to child project, skip it
                                    continue
                                else:
                                    # subdirectory without child projects, rglob it
                                    files_by_projects_dirs[project_dir_path].extend(
                                        path for path in dir_item.rglob("*.py")
                                    )

    return files_by_projects_dirs


def get_project_files(project_dir_path: Path) -> list[Path]:
    files_by_projects = get_files_by_projects([project_dir_path])
    return files_by_projects[project_dir_path]
