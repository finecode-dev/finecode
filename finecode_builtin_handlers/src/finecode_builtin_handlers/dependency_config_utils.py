import pathlib
import tomllib


def get_dependency_name(dependency_str: str) -> str:
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]
    return dependency_str


def process_raw_deps(
    raw_deps: list,
    dependencies: list,
    deps_groups: dict,
    project_def_path: pathlib.Path,
    _seen: set[str] | None = None,
) -> None:
    if _seen is None:
        _seen = set()
    for raw_dep in raw_deps:
        if isinstance(raw_dep, str):
            name = get_dependency_name(raw_dep)
            if name in _seen:
                continue
            _seen.add(name)
            version_or_source = raw_dep[len(name):]
            dependencies.append(
                {
                    "name": name,
                    "version_or_source": version_or_source,
                    "editable": False,
                }
            )
        elif isinstance(raw_dep, dict) and "include-group" in raw_dep:
            included_group_deps = deps_groups.get(raw_dep["include-group"], [])
            process_raw_deps(
                included_group_deps, dependencies, deps_groups,
                project_def_path, _seen,
            )


def collect_transitive_editable_deps(
    dependencies: list[dict],
    ws_editable_packages: dict[str, pathlib.Path],
) -> list[dict]:
    """For each editable dep, read its pyproject.toml and add any of its dependencies
    that are also workspace editable packages, recursively."""
    seen: set[str] = {dep["name"] for dep in dependencies}
    result: list[dict] = []
    queue = [
        dep for dep in dependencies
        if dep.get("editable") and " @ file://" in dep.get("version_or_source", "")
    ]

    while queue:
        dep = queue.pop()
        _, _, path_str = dep["version_or_source"].partition("file://")
        package_dir = pathlib.Path(path_str)

        pyproject_path = package_dir / "pyproject.toml"
        if not pyproject_path.exists():
            continue

        with open(pyproject_path, "rb") as f:
            config = tomllib.load(f)

        dep_names: set[str] = set()
        for dep_str in config.get("project", {}).get("dependencies", []):
            if isinstance(dep_str, str):
                dep_names.add(get_dependency_name(dep_str))
        for group_deps in config.get("dependency-groups", {}).values():
            for group_dep in group_deps:
                if isinstance(group_dep, str):
                    dep_names.add(get_dependency_name(group_dep))

        for pkg_name in dep_names:
            if pkg_name in seen or pkg_name not in ws_editable_packages:
                continue
            resolved = ws_editable_packages[pkg_name]
            new_dep: dict = {
                "name": pkg_name,
                "version_or_source": f" @ file://{resolved.as_posix()}",
                "editable": True,
            }
            seen.add(pkg_name)
            result.append(new_dep)
            queue.append(new_dep)

    return result


__all__ = [
    "collect_transitive_editable_deps",
    "get_dependency_name",
    "process_raw_deps",
]
