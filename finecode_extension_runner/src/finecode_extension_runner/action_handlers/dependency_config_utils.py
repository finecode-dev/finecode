import pathlib
import typing


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


__all__ = [
    "make_project_config_pip_compatible",
    "get_dependency_name",
    "raw_dep_to_dep_dict",
]
