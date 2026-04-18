import pathlib
import tomllib
import typing


def collect_transitive_deps(dependencies: list[dict]) -> list[dict]:
    """For each editable dep with a local file:// source, read its pyproject.toml and
    collect [tool.finecode.deps] entries recursively. Returns additional deps to install."""
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

        if not package_dir.exists():
            continue

        pyproject_path = package_dir / "pyproject.toml"
        if not pyproject_path.exists():
            continue

        with open(pyproject_path, "rb") as f:
            config = tomllib.load(f)

        finecode_deps = config.get("tool", {}).get("finecode", {}).get("deps", {})

        for pkg_name, pkg_params in finecode_deps.items():
            if pkg_name in seen:
                continue
            if not pkg_params.get("editable"):
                continue
            raw_path = pkg_params.get("path")
            if raw_path is None:
                continue
            resolved = pathlib.Path(raw_path)
            if not resolved.is_absolute():
                resolved = (package_dir / resolved).resolve()
            if not resolved.exists():
                continue
            dep_pyproject = resolved / "pyproject.toml"
            if not dep_pyproject.exists():
                raise ValueError(
                    f"Editable dep '{pkg_name}' declared in '{pyproject_path}'"
                    f" points to '{resolved}' which has no pyproject.toml"
                )
            new_dep: dict = {
                "name": pkg_name,
                "version_or_source": f" @ file://{resolved.as_posix()}",
                "editable": True,
            }
            seen.add(pkg_name)
            result.append(new_dep)
            queue.append(new_dep)

    return result


def make_project_config_pip_compatible(
    project_raw_config: dict[str, typing.Any], config_file_path: pathlib.Path
) -> None:
    finecode_config = project_raw_config.get("tool", {}).get("finecode", {})
    # apply changes to dependencies from env configuration to deps groups
    for env_name in finecode_config.get("env", {}).keys():
        make_env_deps_pip_compatible(
            env_name=env_name,
            project_raw_config=project_raw_config,
            config_file_path=config_file_path,
        )


def make_env_deps_pip_compatible(
    env_name: str,
    project_raw_config: dict[str, typing.Any],
    config_file_path: pathlib.Path,
) -> None:
    env_config = (
        project_raw_config.get("tool", {})
        .get("finecode", {})
        .get("env", {})
        .get(env_name, None)
    )
    if env_config is None or "dependencies" not in env_config:
        return

    env_deps_group = project_raw_config.get("dependency-groups", {}).get(env_name, [])
    dependencies = env_config["dependencies"]
    for dep_name, dep_params in dependencies.items():
        # handle 'path'. 'editable' cannot be handled here because dependency
        # specifier doesn't support it. It will read and processed by
        # `install_deps` action
        if "path" in dep_params:
            # replace dependency version / source in dependency group to this path
            #
            # check all dependencies because it can be duplicated: e.g. as explicit
            # dependency and as dependency of action handler.
            dep_indexes_in_group: list[int] = []
            configured_dep_found_in_dep_group = False
            for idx, dep in enumerate(env_deps_group):
                if isinstance(dep, dict):
                    if "include-group" in dep:
                        included_group = dep["include-group"]
                        make_env_deps_pip_compatible(
                            env_name=included_group,
                            project_raw_config=project_raw_config,
                            config_file_path=config_file_path,
                        )
                elif isinstance(dep, str):
                    if get_dependency_name(dep) == dep_name:
                        dep_indexes_in_group.append(idx)
                        configured_dep_found_in_dep_group = True

            resolved_path_to_dep = pathlib.Path(dep_params["path"])
            if not resolved_path_to_dep.is_absolute():
                # resolve relative to project dir where project def file is
                resolved_path_to_dep = config_file_path.parent / resolved_path_to_dep
            new_dep_str_in_group = (
                f"{dep_name} @ file://{resolved_path_to_dep.as_posix()}"
            )
            for idx in dep_indexes_in_group:
                env_deps_group[idx] = new_dep_str_in_group

            if not configured_dep_found_in_dep_group:
                # if dependency has configuration, but was not found in dependency
                # group of environment, still add it, because it can be deeper in the
                # dependency tree and user wants to overwrite it
                env_deps_group.append(new_dep_str_in_group)


def get_dependency_name(dependency_str: str) -> str:
    # simplified way for now: find the first character which is not allowed in package
    # name
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]

    # dependency can consist also just of package name without version
    return dependency_str


def raw_dep_to_dep_dict(raw_dep: str, env_deps_config: dict) -> dict[str, str | bool]:
    name = get_dependency_name(raw_dep)
    version_or_source = raw_dep[len(name) :]
    editable = env_deps_config.get(name, {}).get("editable", False)
    dep_dict = {
        "name": name,
        "version_or_source": version_or_source,
        "editable": editable,
    }
    return dep_dict


def process_raw_deps(
    raw_deps: list,
    env_deps_config: dict,
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
            dep_config = env_deps_config.get(name, {})
            editable = dep_config.get("editable", False)
            if editable and (raw_path := dep_config.get("path")):
                resolved = pathlib.Path(raw_path)
                if not resolved.is_absolute():
                    resolved = (project_def_path.parent / resolved).resolve()
                version_or_source = f" @ file://{resolved.as_posix()}"
            else:
                version_or_source = raw_dep[len(name):]
            dependencies.append(
                {
                    "name": name,
                    "version_or_source": version_or_source,
                    "editable": editable,
                }
            )
        elif isinstance(raw_dep, dict) and "include-group" in raw_dep:
            included_group_deps = deps_groups.get(raw_dep["include-group"], [])
            process_raw_deps(
                included_group_deps, env_deps_config, dependencies, deps_groups,
                project_def_path, _seen,
            )


__all__ = [
    "collect_transitive_deps",
    "make_project_config_pip_compatible",
    "get_dependency_name",
    "process_raw_deps",
    "raw_dep_to_dep_dict",
]
