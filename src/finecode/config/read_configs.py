from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger
from tomlkit import loads as toml_loads

from finecode import context, domain, user_messages
from finecode.config import config_models
from finecode.runner import runner_client, runner_info


async def read_projects_in_dir(
    dir_path: Path, ws_context: context.WorkspaceContext
) -> list[domain.Project]:
    # Find all projects in directory
    # `dir_path` expected to be absolute path
    logger.trace(f"Read directories in {dir_path}")
    new_projects: list[domain.Project] = []
    def_files_generator = dir_path.rglob("pyproject.toml")
    for def_file in def_files_generator:
        # ignore definition files in `__testdata__` directory, projects in test data
        # can be started only in tests, not from outside
        # TODO: make configurable?
        # path to definition file relative to workspace directory in which this
        # definition was found
        def_file_rel_dir_path = def_file.relative_to(dir_path)
        if "__testdata__" in def_file_rel_dir_path.parts:
            logger.debug(
                f"Skip '{def_file}' because it is in test data and it is not a test session"
            )
            continue
        if def_file.parent.name == "finecode_config_dump":
            logger.debug(
                f"Skip '{def_file}' because it is config dump, not real project config"
            )
            continue

        status = domain.ProjectStatus.CONFIG_VALID
        actions: list[domain.Action] | None = None

        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read()).value

        dependency_groups = project_def.get("dependency-groups", {})
        dev_workspace_group = dependency_groups.get("dev_workspace", [])
        finecode_in_dev_workspace = any(
            dep for dep in dev_workspace_group if get_dependency_name(dep) == "finecode"
        )
        if not finecode_in_dev_workspace:
            status = domain.ProjectStatus.NO_FINECODE
            actions = []

        new_project = domain.Project(
            name=def_file.parent.name,
            dir_path=def_file.parent,
            def_path=def_file,
            status=status,
            actions=actions,
            env_configs={},
        )
        ws_context.ws_projects[def_file.parent] = new_project
        new_projects.append(new_project)
    return new_projects


def get_dependency_name(dependency_str: str) -> str:
    # simplified way for now: find the first character which is not allowed in package
    # name
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]

    # dependency can consist also just of package name without version
    return dependency_str


def read_env_configs(project_config: dict[str, Any]) -> dict[str, domain.EnvConfig]:
    env_configs = {}
    env_config_section = (
        project_config.get("tool", {}).get("finecode", {}).get("env", {})
    )
    for env_name, env_raw_config in env_config_section.items():
        if "runner" in env_raw_config:
            runner_raw_config = env_raw_config["runner"]
            if "debug" in runner_raw_config:
                debug = runner_raw_config["debug"]
                runner_config = domain.RunnerConfig(debug=debug)
                env_config = domain.EnvConfig(runner_config=runner_config)
                env_configs[env_name] = env_config

    # add default configs
    deps_groups = project_config.get("dependency-groups", {})
    for group_name in deps_groups.keys():
        if group_name not in env_configs:
            runner_config = domain.RunnerConfig(debug=False)
            env_config = domain.EnvConfig(runner_config=runner_config)
            env_configs[group_name] = env_config

    return env_configs


async def read_project_config(
    project: domain.Project,
    ws_context: context.WorkspaceContext,
    resolve_presets: bool = True,
) -> None:
    # this function requires running project extension runner to get configuration
    # from it
    if project.def_path.name == "pyproject.toml":
        with open(project.def_path, "rb") as pyproject_file:
            # TODO: handle error if toml is invalid
            project_def = toml_loads(pyproject_file.read()).value
        # TODO: validate that finecode is installed?

        base_config_path = Path(__file__).parent.parent / "base_config.toml"
        # TODO: cache instead of reading each time
        with open(base_config_path, "r") as base_config_file:
            base_config = toml_loads(base_config_file.read()).value
        project_config = {}
        _merge_projects_configs(
            project_config, project.def_path, base_config, base_config_path
        )

        finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
        if finecode_raw_config and resolve_presets:
            try:
                presets = [
                    config_models.FinecodePresetDefinition(**raw_preset)
                    for raw_preset in finecode_raw_config.get("presets", [])
                ]
            except config_models.ValidationError as exception:
                raise config_models.ConfigurationError(str(exception))

            # all presets expected to be in `dev_no_runtime` environment
            project_runners = ws_context.ws_projects_extension_runners[project.dir_path]
            # TODO: can it be the case that there is no such runner?
            dev_no_runtime_runner = project_runners["dev_no_runtime"]
            new_config = await collect_config_from_py_presets(
                presets_sources=[preset.source for preset in presets],
                def_path=project.def_path,
                runner=dev_no_runtime_runner,
            )
            if new_config is not None:
                _merge_projects_configs(
                    project_config, project.def_path, new_config, project.def_path
                )

        _merge_projects_configs(
            project_config, project.def_path, project_def, project.def_path
        )
        # `_merge_projects_configs` merges only finecode config. Copy all other keys as
        # is
        for key, value in project_def.items():
            if key != "tool":
                project_config[key] = value
        tool_raw_config = project_def.get("tool", None)
        if tool_raw_config is not None:
            if "tool" not in project_config:
                project_config["tool"] = {}
            project_tool_config = project_config["tool"]
            for key, value in tool_raw_config.items():
                if key != "finecode":
                    project_tool_config[key] = value

        # add runtime dependency group if it's not explicitly declared
        add_runtime_dependency_group_if_new(project_config)

        add_extension_runner_to_dependencies(project_config)

        merge_handlers_dependencies_into_groups(project_config)

        ws_context.ws_projects_raw_configs[project.dir_path] = project_config

        env_configs = read_env_configs(project_config=project_config)
        project.env_configs = env_configs
    else:
        logger.info(
            f"Project definition of type {project.def_path.name} is not supported yet"
        )


class PresetToProcess(NamedTuple):
    source: str
    project_def_path: Path


async def get_preset_project_path(
    preset: PresetToProcess, def_path: Path, runner: runner_info.ExtensionRunnerInfo
) -> Path | None:
    logger.trace(f"Get preset project path: {preset.source}")

    try:
        resolve_path_result = await runner_client.resolve_package_path(
            runner, preset.source
        )
    except runner_client.BaseRunnerRequestException as error:
        await user_messages.error(f"Failed to get preset project path: {error.message}")
        return None
    try:
        preset_project_path = Path(resolve_path_result["packagePath"])
    except KeyError:
        raise ValueError(f"Preset source cannot be resolved: {preset.source}")

    logger.trace(f"Got: {preset.source} -> {preset_project_path}")
    return preset_project_path


def read_preset_config(
    config_path: Path, preset_id: str
) -> tuple[dict[str, Any], config_models.PresetDefinition]:
    # preset_id is used only for logs to make them more useful
    logger.trace(f"Read preset config: {preset_id}")
    if not config_path.exists():
        # if package is installed in editable mode, we will get path to root directory
        # of the package, not to source directory. In such case check both flat and
        # src layouts of the package
        config_dir_path = config_path.parent
        flat_path_to_src = config_dir_path / preset_id
        if flat_path_to_src.exists():
            config_path = flat_path_to_src / "preset.toml"
        else:
            src_path_to_src = config_dir_path / "src" / preset_id
            if src_path_to_src.exists():
                config_path = src_path_to_src / "preset.toml"
            else:
                raise config_models.ConfigurationError(
                    f"preset.toml not found in project '{preset_id}'"
                )

    with open(config_path, "rb") as preset_toml_file:
        preset_toml = toml_loads(preset_toml_file.read()).value

    try:
        presets = preset_toml["tool"]["finecode"]["presets"]
    except KeyError:
        presets = []

    preset_config = config_models.PresetDefinition(extends=presets)

    logger.trace(f"Reading preset config finished: {preset_id}")
    return (preset_toml, preset_config)


async def collect_config_from_py_presets(
    presets_sources: list[str], def_path: Path, runner: runner_info.ExtensionRunnerInfo
) -> dict[str, Any] | None:
    config: dict[str, Any] | None = None
    processed_presets: set[str] = set()
    presets_to_process: set[PresetToProcess] = set(
        [
            PresetToProcess(source=preset_source, project_def_path=def_path)
            for preset_source in presets_sources
        ]
    )
    while len(presets_to_process) > 0:
        preset = presets_to_process.pop()
        processed_presets.add(preset.source)

        preset_project_path = await get_preset_project_path(
            preset=preset, def_path=def_path, runner=runner
        )
        if preset_project_path is None:
            logger.trace(f"Path of preset {preset.source} not found")
            raise config_models.ConfigurationError(
                f"Path of preset {preset.source} in project {def_path.parent} not found"
            )

        preset_toml_path = preset_project_path / "preset.toml"
        preset_toml, preset_config = read_preset_config(preset_toml_path, preset.source)
        if config is None:
            config = preset_toml
        else:
            _merge_projects_configs(config, def_path, preset_toml, preset_project_path)
        new_presets_sources = (
            set([extend.source for extend in preset_config.extends]) - processed_presets
        )
        for new_preset_source in new_presets_sources:
            presets_to_process.add(
                PresetToProcess(
                    source=new_preset_source,
                    project_def_path=def_path,
                )
            )

    return config


def _merge_object_array_by_key(
    existing_array: list[dict[str, Any]],
    new_array: list[dict[str, Any]],
    key_field: str,
) -> None:
    """
    Merges object arrays by a specified key field.

    For each object in new_array:
    - If an object with the same key_field value exists in existing_array, deep merge them
    - If no object with that key_field value exists, append the new object

    Args:
        existing_array: The array to merge into
        new_array: The array to merge from
        key_field: The field name to use as the merge key (e.g., 'name', 'source')
    """
    # Create a lookup map for existing objects by the key field
    existing_by_key = {}
    for i, obj in enumerate(existing_array):
        if isinstance(obj, dict) and key_field in obj:
            existing_by_key[obj[key_field]] = i

    # Process each new object
    for new_obj in new_array:
        if not isinstance(new_obj, dict) or key_field not in new_obj:
            # If the object doesn't have the key field, just append it
            existing_array.append(new_obj)
            continue

        obj_key = new_obj[key_field]
        if obj_key in existing_by_key:
            # Merge with existing object
            existing_index = existing_by_key[obj_key]
            existing_obj = existing_array[existing_index]
            _deep_merge_dicts(existing_obj, new_obj)
        else:
            # Add new object
            existing_array.append(new_obj)


def _deep_merge_dicts(target: dict[str, Any], source: dict[str, Any]) -> None:
    """
    Deep merge source dict into target dict.
    Arrays are replaced entirely (not merged), following TOML semantics.
    """
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge_dicts(target[key], value)
        else:
            target[key] = value


def _merge_projects_configs(
    config1: dict[str, Any],
    config1_filepath: Path,
    config2: dict[str, Any],
    config2_filepath: Path,
) -> None:
    # merge config2 in config1 without overwriting
    if "tool" not in config1:
        config1["tool"] = {}
    if "finecode" not in config1["tool"]:
        config1["tool"]["finecode"] = {}

    tool_finecode_config1 = config1["tool"]["finecode"]
    tool_finecode_config2 = config2.get("tool", {}).get("finecode", {})

    for key, value in tool_finecode_config2.items():
        if key == "action":
            # first process actions explicitly to merge correct configs
            assert isinstance(value, dict)
            if key not in tool_finecode_config1:
                tool_finecode_config1[key] = {}
            for action_name, action_info in value.items():
                if action_name not in tool_finecode_config1[key]:
                    # new action, just add as it is
                    tool_finecode_config1[key][action_name] = action_info
                else:
                    # action with the same name, merge
                    if "config" in action_info:
                        if "config" not in tool_finecode_config1[key][action_name]:
                            tool_finecode_config1[key][action_name]["config"] = {}

                        action_config = tool_finecode_config1[key][action_name][
                            "config"
                        ]
                        action_config.update(action_info["config"])

                    # Handle handlers array merge by name
                    if "handlers" in action_info:
                        if "handlers" not in tool_finecode_config1[key][action_name]:
                            tool_finecode_config1[key][action_name]["handlers"] = []

                        existing_handlers = tool_finecode_config1[key][action_name][
                            "handlers"
                        ]
                        new_handlers = action_info["handlers"]

                        # Merge handlers by name
                        _merge_object_array_by_key(
                            existing_handlers, new_handlers, "name"
                        )
        elif key == "action_handler":
            # Handle action_handler array merge by source
            if key not in tool_finecode_config1:
                tool_finecode_config1[key] = []

            existing_action_handlers = tool_finecode_config1[key]
            # Ensure value is a list
            if isinstance(value, list):
                new_action_handlers = value
                # Merge action_handlers by source
                _merge_object_array_by_key(
                    existing_action_handlers, new_action_handlers, "source"
                )
            else:
                # If it's not a list, just set it directly (shouldn't happen with TOML arrays but be safe)
                tool_finecode_config1[key] = value
        elif key == "env":
            if "env" not in tool_finecode_config1:
                tool_finecode_config1["env"] = {}

            all_envs_config1 = tool_finecode_config1["env"]

            for env_name, env_config2 in value.items():
                if env_name not in all_envs_config1:
                    all_envs_config1[env_name] = env_config2
                else:
                    # merge env configs
                    env_config1 = all_envs_config1[env_name]
                    if "dependencies" in env_config2:
                        if "dependencies" not in env_config1:
                            env_config1["dependencies"] = env_config2["dependencies"]
                        else:
                            # merge dependencies
                            env_config1_deps = env_config1["dependencies"]
                            for dependency_name, dependency in env_config2[
                                "dependencies"
                            ].items():
                                if dependency_name not in env_config1_deps:
                                    env_config1_deps[dependency_name] = dependency
                                else:
                                    if "path" in dependency:
                                        new_path = dependency["path"]
                                        if new_path.startswith("."):
                                            abs_path = (
                                                config2_filepath.parent / new_path
                                            )
                                            new_rel_path = abs_path.relative_to(
                                                config1_filepath.parent
                                            )
                                            new_path = new_rel_path.as_posix()
                                        env_config1_deps[dependency_name][
                                            "path"
                                        ] = new_path
                                    if "editable" in dependency:
                                        env_config1_deps[dependency_name][
                                            "editable"
                                        ] = dependency["editable"]
                    if "runner" in env_config2:
                        if "runner" not in env_config1:
                            env_config1["runner"] = {}
                        env_config1_runner = env_config1["runner"]
                        env_config2_runner = env_config2["runner"]

                        if "debug" in env_config2_runner:
                            env_config1_runner["debug"] = env_config2_runner["debug"]
        elif key in config1:
            tool_finecode_config1[key].update(value)
        else:
            tool_finecode_config1[key] = value


def add_action_to_config_if_new(
    raw_config: dict[str, Any], action: domain.Action
) -> None:
    # adds action to raw config if it is not defined yet. Existing action will be not
    # overwritten
    tool_config = add_or_get_dict_key_value(raw_config, "tool", {})
    finecode_config = add_or_get_dict_key_value(tool_config, "finecode", {})
    action_config = add_or_get_dict_key_value(finecode_config, "action", {})
    if action.name not in action_config:
        action_raw_dict = {
            "source": action.source,
            "handlers": [handler_to_dict(handler) for handler in action.handlers],
        }
        action_config[action.name] = action_raw_dict

    # example of action definition:
    # [tool.finecode.action.text_document_inlay_hint]
    # source = "finecode_extension_api.actions.ide.text_document_inlay_hint.TextDocumentInlayHintAction"
    # handlers = [
    #     { name = 'module_exports_inlay_hint', source = 'fine_python_module_exports.extension.get_document_inlay_hints', env = "dev_no_runtime", dependencies = [
    #         "fine_python_module_exports @ git+https://github.com/finecode-dev/finecode.git#subdirectory=extensions/fine_python_module_exports",
    #     ] },
    # ]


def add_or_get_dict_key_value(
    dict_obj: dict[str, Any], key: str, default_value: Any
) -> Any:
    if key not in dict_obj:
        value = default_value
        dict_obj[key] = value
    else:
        value = dict_obj[key]

    return value


def handler_to_dict(handler: domain.ActionHandler) -> dict[str, str | list[str]]:
    return {
        "name": handler.name,
        "source": handler.source,
        "env": handler.env,
        "dependencies": handler.dependencies,
    }


def add_runtime_dependency_group_if_new(project_config: dict[str, Any]) -> None:
    runtime_dependencies = project_config.get("project", {}).get("dependencies", [])

    deps_groups = add_or_get_dict_key_value(project_config, "dependency-groups", {})
    if "runtime" not in deps_groups:
        deps_groups["runtime"] = runtime_dependencies


def merge_handlers_dependencies_into_groups(project_config: dict[str, Any]) -> None:
    # tool.finecode.action.<action_name>.handlers[x].dependencies
    actions_dict = project_config.get("tool", {}).get("finecode", {}).get("action", {})
    if "dependency-groups" not in project_config:
        project_config["dependency-groups"] = {}
    deps_groups = project_config["dependency-groups"]

    for action_info in actions_dict.values():
        action_handlers = action_info.get("handlers", [])

        for handler in action_handlers:
            handler_env = handler.get("env", None)
            if handler_env is None:
                logger.warning(f"Handler {handler} has no env, skip it")
                continue
            deps = handler.get("dependencies", [])

            if handler_env not in deps_groups:
                deps_groups[handler_env] = []

            env_deps = deps_groups[handler_env]
            # should we remove duplicates here?
            env_deps += deps

    # remove duplicates in dependencies because multiple handlers can have the same
    # dependencies / be from the same package
    for group_name in deps_groups.keys():
        deps_list = deps_groups[group_name]

        # another possibility would be to use `ordered_set.OrderedSet`, but dependency
        # list can contain not only strings, but also dictionaries like
        # `{ 'include-group': 'runtime' }` which are not hashable
        unique_deps = []
        for dep in deps_list:
            if dep not in unique_deps:
                unique_deps.append(dep)

        deps_groups[group_name] = unique_deps


def add_extension_runner_to_dependencies(project_config: dict[str, Any]) -> None:
    try:
        deps_groups = project_config["dependency-groups"]
    except KeyError:
        return

    finecode_version = metadata.version("finecode")

    for group_name, group_packages in deps_groups.items():
        if group_name == "dev_workspace":
            # skip `dev_workspace` because it contains finecode already
            continue

        group_packages.append(f"finecode_extension_runner == {finecode_version}")
