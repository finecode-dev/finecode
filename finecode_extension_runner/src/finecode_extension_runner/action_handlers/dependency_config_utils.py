import pathlib
import typing


def make_project_config_pip_compatible(
    project_raw_config: dict[str, typing.Any], config_file_path: pathlib.Path
) -> None:
    # TODO: what to do with included groups in dependency groups? Inherit config from its env?
    finecode_config = project_raw_config.get("tool", {}).get("finecode", {})
    # apply changes to dependencies from env configuration to deps groups
    for env_name, env_config in finecode_config.get("env", {}).items():
        if "dependencies" not in env_config:
            continue

        env_deps_group = project_raw_config.get("dependency-groups", {}).get(
            env_name, []
        )
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
                for idx, dep in enumerate(env_deps_group):
                    # check for string because dependency can be also dictionary like '{ "include-group": "runtime"}'
                    if isinstance(dep, str) and get_dependency_name(dep) == dep_name:
                        dep_indexes_in_group.append(idx)

                if len(dep_indexes_in_group) == 0:
                    continue

                resolved_path_to_dep = pathlib.Path(dep_params["path"])
                if not resolved_path_to_dep.is_absolute():
                    # resolve relative to project dir where project def file is
                    resolved_path_to_dep = (
                        config_file_path.parent / resolved_path_to_dep
                    )
                new_dep_str_in_group = (
                    f"{dep_name} @ file://{resolved_path_to_dep.as_posix()}"
                )
                for idx in dep_indexes_in_group:
                    env_deps_group[idx] = new_dep_str_in_group


def get_dependency_name(dependency_str: str) -> str:
    # simplified way for now: find the first character which is not allowed in package
    # name
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]

    # dependency can consist also just of package name without version
    return dependency_str


class FailedToGetDependencies(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


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
