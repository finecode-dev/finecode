# docs: docs/configuration.md
import os
from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple


import cattrs
from finecode import user_messages
from finecode._converter import converter as _converter
from finecode.wm_server import context, domain
from finecode.wm_server.config import config_models
from finecode.wm_server.runner import runner_client
from loguru import logger
from tomlkit import loads as toml_loads


def read_project_finecode_config(project_dir: Path) -> dict | None:
    """Read finecode.toml at the project root.

    Returns the parsed mapping, or None if the file does not exist.
    Raises ConfigurationError if the file is malformed.
    """
    finecode_toml_path = project_dir / "finecode.toml"
    if not finecode_toml_path.exists():
        return None
    try:
        with open(finecode_toml_path, "rb") as f:
            return toml_loads(f.read()).unwrap()
    except Exception as e:
        raise config_models.ConfigurationError(
            f"Failed to parse {finecode_toml_path}: {e}"
        )


async def read_projects_in_dir(
    dir_path: Path, ws_context: context.WorkspaceContext
) -> list[domain.Project]:
    # Find all projects in directory
    # `dir_path` expected to be absolute path
    #
    # Directories that are never FineCode projects and are often large.
    # Skipping them avoids traversing thousands of files in virtualenvs,
    # caches, and third-party package trees.
    _SKIP_DIRS = {
        ".venv", ".venvs",
        ".git",
        "node_modules",
        "__pycache__",
        ".tox",
        "dist", "build",
        ".mypy_cache", ".ruff_cache", ".pytest_cache",
    }

    logger.trace(f"Read directories in {dir_path}")
    new_projects: list[domain.Project] = []
    def_files: list[Path] = []
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if "pyproject.toml" in files:
            def_files.append(Path(root) / "pyproject.toml")

    for def_file in def_files:
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

        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read()).unwrap()

        finecode_toml_exists = (def_file.parent / "finecode.toml").exists()
        has_pyproject_finecode = project_def.get("tool", {}).get("finecode") is not None
        if finecode_toml_exists and has_pyproject_finecode:
            raise config_models.ConfigurationError(
                f"Project FineCode configuration is defined in two places for project\n"
                f"{def_file.parent}:\n"
                f"  - {def_file.parent / 'finecode.toml'}\n"
                f"  - {def_file} ([tool.finecode.*])\n"
                f"Pick one location and remove the other. The two files cannot be\n"
                f"combined — one wins entirely."
            )

        dependency_groups = project_def.get("dependency-groups", {})
        dev_workspace_group = dependency_groups.get("dev_workspace", [])
        finecode_in_dev_workspace = any(
            dep for dep in dev_workspace_group if get_dependency_name(dep) == "finecode"
        )
        if not finecode_in_dev_workspace:
            status = domain.ProjectStatus.NO_FINECODE

        is_new_project = def_file.parent not in ws_context.ws_projects
        if is_new_project:
            new_project = domain.Project(
                name=project_def.get("project", {}).get("name"),
                dir_path=def_file.parent,
                def_path=def_file,
                status=status,
            )
            ws_context.ws_projects[def_file.parent] = new_project
            new_projects.append(new_project)
        else:
            # Preserve existing collected/resolved state — only update status in case
            # the finecode dependency was added or removed since the last scan.
            ws_context.ws_projects[def_file.parent].status = status
    return new_projects


def get_dependency_name(dependency_str: str) -> str:
    # simplified way for now: find the first character which is not allowed in package
    # name
    for idx, ch in enumerate(dependency_str):
        if not ch.isalnum() and ch not in "-_":
            return dependency_str[:idx]

    # dependency can consist also just of package name without version
    return dependency_str


def _read_er_logging_config(raw: dict[str, Any]) -> config_models.ErLoggingConfig:
    logging_raw = raw.get("logging", {})
    default_level = logging_raw.get("default_level", "INFO")
    log_groups = dict(logging_raw.get("log_groups", {}))
    return config_models.ErLoggingConfig(default_level=default_level, log_groups=log_groups)


def _resolve_er_logging_config(
    project_config: dict[str, Any], env_name: str
) -> config_models.ErLoggingConfig:
    """Merge project-level fallback with per-env override from [tool.finecode.er]."""
    er_section = project_config.get("tool", {}).get("finecode", {}).get("er", {})

    fallback = _read_er_logging_config(er_section)

    env_raw = er_section.get("envs", {}).get(env_name, {})
    if not env_raw:
        return _apply_er_env_var_overrides(fallback, env_name)

    env_logging_raw = env_raw.get("logging", {})
    merged_level = env_logging_raw.get("default_level", fallback.default_level)
    merged_groups = {**fallback.log_groups, **dict(env_logging_raw.get("log_groups", {}))}
    merged = config_models.ErLoggingConfig(default_level=merged_level, log_groups=merged_groups)
    return _apply_er_env_var_overrides(merged, env_name)


def _apply_er_env_var_overrides(
    config: config_models.ErLoggingConfig, env_name: str
) -> config_models.ErLoggingConfig:
    import os

    def _env_key(name: str) -> str:
        return name.upper().replace("-", "_").replace(".", "_")

    env_key = _env_key(env_name)

    level = (
        os.environ.get(f"FINECODE_ER_ENV_{env_key}_LOG_LEVEL")
        or os.environ.get("FINECODE_ER_LOG_LEVEL")
        or config.default_level
    )

    groups = dict(config.log_groups)
    for var, value in os.environ.items():
        prefix = f"FINECODE_ER_ENV_{env_key}_LOG_GROUP_"
        if var.startswith(prefix):
            group_key = var[len(prefix):].lower().replace("_", ".")
            groups[group_key] = value
    for var, value in os.environ.items():
        prefix = "FINECODE_ER_LOG_GROUP_"
        if var.startswith(prefix) and not var.startswith(f"FINECODE_ER_ENV_"):
            group_key = var[len(prefix):].lower().replace("_", ".")
            groups.setdefault(group_key, value)

    return config_models.ErLoggingConfig(default_level=level, log_groups=groups)


def read_wm_logging_config(workspace_root: Path) -> config_models.ErLoggingConfig:
    """Read WM logging config from [workspace.wm.logging] in finecode-workspace.toml.

    Env vars FINECODE_WM_LOG_GROUP_<GROUP>=LEVEL override file values (uppercase
    group name with dots replaced by underscores, e.g. FINECODE_WM_LOG_GROUP_FINECODE_JSONRPC=DEBUG).
    """
    import os

    log_groups: dict[str, str] = {}

    ws_config_path = workspace_root / "finecode-workspace.toml"
    if ws_config_path.exists():
        try:
            with open(ws_config_path, "rb") as f:
                ws_config = toml_loads(f.read()).unwrap()
            logging_raw = ws_config.get("workspace", {}).get("wm", {}).get("logging", {})
            log_groups = dict(logging_raw.get("log_groups", {}))
        except Exception:
            pass

    for var, value in os.environ.items():
        if var.startswith("FINECODE_WM_LOG_GROUP_"):
            group_key = var[len("FINECODE_WM_LOG_GROUP_"):].lower().replace("_", ".")
            log_groups[group_key] = value

    return config_models.ErLoggingConfig(log_groups=log_groups)


def read_wm_telemetry_config(workspace_root: Path) -> config_models.WmTelemetryConfig:
    """Read WM telemetry config from [workspace.wm.telemetry] in finecode-workspace.toml.

    FINECODE_OTLP_ENDPOINT env var overrides the file value (highest priority).
    """
    import os

    otlp_endpoint: str | None = None

    ws_config_path = workspace_root / "finecode-workspace.toml"
    if ws_config_path.exists():
        try:
            with open(ws_config_path, "rb") as f:
                ws_config = toml_loads(f.read()).unwrap()
            telemetry_raw = ws_config.get("workspace", {}).get("wm", {}).get("telemetry", {})
            otlp_endpoint = telemetry_raw.get("otlp_endpoint", None)
        except Exception:
            pass

    otlp_endpoint = os.environ.get("FINECODE_OTLP_ENDPOINT") or otlp_endpoint

    return config_models.WmTelemetryConfig(otlp_endpoint=otlp_endpoint)


def read_env_configs(project_config: dict[str, Any]) -> dict[str, domain.EnvConfig]:
    env_configs: dict[str, domain.EnvConfig] = {}

    er_section = project_config.get("tool", {}).get("finecode", {}).get("er", {})
    for env_name, env_raw in er_section.get("envs", {}).items():
        if not isinstance(env_raw, dict):
            continue
        debug = env_raw.get("debug", False)
        logging_config = _resolve_er_logging_config(project_config, env_name)
        runner_config = domain.RunnerConfig(debug=debug, logging=logging_config)
        env_configs[env_name] = domain.EnvConfig(runner_config=runner_config)

    # add default configs for dependency-group envs not explicitly listed under er
    deps_groups = project_config.get("dependency-groups", {})
    for group_name in deps_groups.keys():
        if group_name not in env_configs:
            logging_config = _resolve_er_logging_config(project_config, group_name)
            runner_config = domain.RunnerConfig(debug=False, logging=logging_config)
            env_configs[group_name] = domain.EnvConfig(runner_config=runner_config)

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
            project_def = toml_loads(pyproject_file.read()).unwrap()
        # TODO: validate that finecode is installed?

        finecode_toml_raw = read_project_finecode_config(project.def_path.parent)
        if finecode_toml_raw is not None:
            finecode_section = dict(finecode_toml_raw.get("finecode", {}))
            if "workspace" in finecode_section or "workspace" in finecode_toml_raw:
                raise config_models.ConfigurationError(
                    f"The [workspace] table is not allowed in "
                    f"{project.def_path.parent / 'finecode.toml'}. "
                    f"Workspace configuration must live in finecode-workspace.toml."
                )
            if "tool" not in project_def:
                project_def["tool"] = {}
            project_def["tool"]["finecode"] = finecode_section

        project_config = {}

        # fine_envs is always loaded as a mandatory preset; user presets are loaded
        # only when resolve_presets=True. Both require a dev_workspace runner.
        finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
        preset_sources: list[str] = ["fine_envs"]
        if finecode_raw_config and resolve_presets:
            try:
                user_presets = [
                    _converter.structure(raw_preset, config_models.FinecodePresetDefinition)
                    for raw_preset in finecode_raw_config.get("presets", [])
                ]
            except cattrs.ClassValidationError as exception:
                raise config_models.ConfigurationError(str(exception))
            preset_sources += [preset.source for preset in user_presets]

        # TODO: can it be the case that there is no such runner? 
        dev_workspace_runner = ws_context.ws_projects_extension_runners.get(
            project.dir_path, {}
        ).get("dev_workspace")
        if dev_workspace_runner is not None:
            new_config = await collect_config_from_py_presets(
                presets_sources=preset_sources,
                def_path=project.def_path,
                runner=dev_workspace_runner,
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

        finecode_section = project_config.get("tool", {}).get("finecode", {})
        actions = _structure_actions(finecode_section.get("action", {}))
        services = _structure_services(finecode_section.get("service", []))

        deps_groups: dict[str, list[Any]] = project_config.setdefault(
            "dependency-groups", {}
        )
        merge_handlers_dependencies_into_groups(actions, deps_groups)
        merge_services_dependencies_into_groups(services, deps_groups)
        _deduplicate_deps_groups(deps_groups)
        # add extension runner after merging handlers dependencies into groups
        # because env may be missing in dependency-groups and be used in handlers
        add_extension_runner_to_dependencies(project_config)

        ws_context.ws_projects_raw_configs[project.dir_path] = project_config
    else:
        logger.info(
            f"Project definition of type {project.def_path.name} is not supported yet"
        )


class PresetToProcess(NamedTuple):
    source: str
    project_def_path: Path


async def get_preset_project_path(
    preset: PresetToProcess, def_path: Path, runner: runner_client.ExtensionRunnerInfo
) -> Path:
    logger.trace(f"Get preset project path: {preset.source}")

    try:
        resolve_path_result = await runner_client.resolve_package_path(
            runner, preset.source
        )
    except runner_client.BaseRunnerRequestException as error:
        error_message = error.message
        lower_message = error_message.lower()
        if "cannot find package" in lower_message or "no module named" in lower_message:
            raise config_models.PresetPackageNotInstalledError(
                "Preset "
                f"{preset.source} is referenced in project {def_path.parent}, "
                "but this preset package is not installed in the dev_workspace "
                "environment. "
                f"Runner error: {error_message}"
            )

        await user_messages.error(f"Failed to get preset project path: {error_message}")
        raise config_models.ConfigurationError(
            "Failed to resolve preset package path "
            f"for {preset.source} in project {def_path.parent}: {error_message}"
        )
    try:
        preset_project_path = Path(resolve_path_result["packagePath"])
    except KeyError as exception:
        raise config_models.ConfigurationError(
            f"Preset source cannot be resolved — ER response missing 'packagePath': {preset.source}"
        ) from exception

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
        preset_toml = toml_loads(preset_toml_file.read()).unwrap()

    try:
        presets = preset_toml["tool"]["finecode"]["presets"]
    except KeyError:
        presets = []
    try:
        preset_config = config_models.PresetDefinition(
            extends=[
                _converter.structure(raw_preset, config_models.FinecodePresetDefinition)
                for raw_preset in presets
            ]
        )
    except cattrs.ClassValidationError as exception:
        raise config_models.ConfigurationError(
            f"Invalid preset extension in {preset_id}: {exception}"
        )

    logger.trace(f"Reading preset config finished: {preset_id}")
    return (preset_toml, preset_config)


async def collect_config_from_py_presets(
    presets_sources: list[str],
    def_path: Path,
    runner: runner_client.ExtensionRunnerInfo,
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

        preset_toml_path = preset_project_path / "preset.toml"
        preset_toml, preset_config = read_preset_config(preset_toml_path, preset.source)
        if config is None:
            # use merge instead of just assigning config, because merge not only merges
            # configs, but also adapts relative pathes etc.
            config = {}
            _merge_projects_configs(config, def_path, preset_toml, preset_toml_path, is_from_preset=True)
        else:
            _merge_projects_configs(config, def_path, preset_toml, preset_toml_path, is_from_preset=True)
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


def _merge_override_specs(existing: list[str], new: list[str]) -> list[str]:
    """Merge two PEP 508 override spec lists; later list wins per canonical package name."""
    import re

    def _canonical(spec: str) -> str:
        m = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", spec.strip())
        name = m.group(1) if m else spec.strip()
        return re.sub(r"[-_.]+", "-", name).lower()

    merged: dict[str, str] = {_canonical(s): s for s in existing}
    for s in new:
        merged[_canonical(s)] = s
    return list(merged.values())


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
    is_from_preset: bool = False,
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
            if not isinstance(value, dict):
                raise config_models.ConfigurationError(
                    f"[tool.finecode.action] must be a TOML table, got {type(value).__name__}"
                )
            if key not in tool_finecode_config1:
                tool_finecode_config1[key] = {}
            for action_name, action_info in value.items():
                if action_name not in tool_finecode_config1[key]:
                    # new action — normalize dict-keyed handlers before storing
                    if isinstance(action_info.get("handlers"), dict):
                        action_info = dict(action_info)
                        action_info["handlers"] = [
                            {"name": name, **fields}
                            for name, fields in action_info["handlers"].items()
                        ]
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
                        handlers_mode = action_info.get("handlers_mode", "merge")
                        if handlers_mode == "replace":
                            tool_finecode_config1[key][action_name]["handlers"] = (
                                action_info["handlers"]
                            )
                        else:
                            if (
                                "handlers"
                                not in tool_finecode_config1[key][action_name]
                            ):
                                tool_finecode_config1[key][action_name]["handlers"] = []

                            existing_handlers = tool_finecode_config1[key][action_name][
                                "handlers"
                            ]
                            new_handlers = action_info["handlers"]

                            # Dict-keyed shorthand:
                            # [tool.finecode.action.X.handlers.handler_name]
                            # <any handler field> = value
                            # Normalize to [{"name": handler_name, <fields>}]
                            if isinstance(new_handlers, dict):
                                new_handlers = [
                                    {"name": name, **fields}
                                    for name, fields in new_handlers.items()
                                ]

                            # Merge handlers by name
                            _merge_object_array_by_key(
                                existing_handlers, new_handlers, "name"
                            )
        elif key == "service":
            if key not in tool_finecode_config1:
                tool_finecode_config1[key] = []
            existing = tool_finecode_config1[key]
            if isinstance(value, list):
                _merge_object_array_by_key(existing, value, "interface")
            else:
                tool_finecode_config1[key] = value
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
                                        env_config1_deps[dependency_name]["path"] = (
                                            dependency["path"]
                                        )
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

                for updated_dep_name, updated_dep_config in env_config2.get(
                    "dependencies", {}
                ).items():
                    if "path" in updated_dep_config:
                        # if path is provided in the config2 dependency,
                        # it overwrites path in config1 and relative path
                        # must be adjusted
                        new_path = updated_dep_config["path"]
                        if new_path.startswith("."):
                            abs_path = (config2_filepath.parent / new_path).resolve()
                            if is_from_preset and not abs_path.exists():
                                logger.debug(
                                    f"Skipping preset-contributed dep '{updated_dep_name}': "
                                    f"path '{abs_path}' does not exist"
                                )
                                continue
                            new_rel_path = abs_path.relative_to(
                                config1_filepath.parent, walk_up=True
                            )
                            new_path = new_rel_path.as_posix()
                            # .relative_to() doesn't add './' for items in the current
                            # directory, but FineCode needs it to distinguish relative
                            # path from absolute one
                            if not new_path.startswith("."):
                                new_path = "./" + new_path
                            all_envs_config1[env_name]["dependencies"][
                                updated_dep_name
                            ]["path"] = new_path
        elif key == "extension":
            if "extension" not in tool_finecode_config1:
                tool_finecode_config1["extension"] = {}
            ext_config1 = tool_finecode_config1["extension"]
            for ext_name, ext_data in value.items():
                if ext_name not in ext_config1:
                    ext_config1[ext_name] = dict(ext_data)
                else:
                    existing_overrides = ext_config1[ext_name].get("dependencies_override", [])
                    new_overrides = ext_data.get("dependencies_override", [])
                    ext_config1[ext_name]["dependencies_override"] = _merge_override_specs(
                        existing_overrides, new_overrides
                    )
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
    # source = "fine_inlay_hints.TextDocumentInlayHintAction"
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

    # add root package to runtime env if it is not there yet. It is done here and not
    # in package installer, because runtime deps group can be included in other groups
    # and root package should be installed in them as well
    root_package_name = project_config.get("project", {}).get("name", None)
    if root_package_name is None:
        raise config_models.ConfigurationError("project.name not found in config")
    root_package_in_runtime_deps = any(
        dep
        for dep in runtime_dependencies
        if get_dependency_name(dep) == root_package_name
    )
    if not root_package_in_runtime_deps:
        runtime_dependencies.insert(0, root_package_name)

        # make editable. Example:
        # [tool.finecode.env.runtime.dependencies]
        # package_name = { path = "./", editable = true }
        if "tool" not in project_config:
            project_config["tool"] = {}
        tool_config = project_config["tool"]
        if "finecode" not in tool_config:
            tool_config["finecode"] = {}
        finecode_config = tool_config["finecode"]
        if "env" not in finecode_config:
            finecode_config["env"] = {}
        finecode_env_config = finecode_config["env"]
        if "runtime" not in finecode_env_config:
            finecode_env_config["runtime"] = {}
        runtime_env_config = finecode_env_config["runtime"]
        if "dependencies" not in runtime_env_config:
            runtime_env_config["dependencies"] = {}
        runtime_env_deps = runtime_env_config["dependencies"]
        if root_package_name not in runtime_env_deps:
            runtime_env_deps[root_package_name] = {"path": "./", "editable": True}

    deps_groups = add_or_get_dict_key_value(project_config, "dependency-groups", {})
    if "runtime" not in deps_groups:
        deps_groups["runtime"] = runtime_dependencies


def _structure_actions(
    actions_raw: dict[str, Any],
) -> dict[str, config_models.ActionDefinition]:
    actions: dict[str, config_models.ActionDefinition] = {}
    for action_name, action_def_raw in actions_raw.items():
        try:
            actions[action_name] = _converter.structure(
                action_def_raw, config_models.ActionDefinition
            )
        except cattrs.ClassValidationError as exception:
            errors = "\n  ".join(cattrs.transform_error(exception))
            raise config_models.ConfigurationError(
                f"Invalid configuration for action '{action_name}':\n  {errors}"
            ) from exception
    return actions


def _structure_services(
    services_raw: list[Any],
) -> list[config_models.ServiceDefinition]:
    services: list[config_models.ServiceDefinition] = []
    for i, service_def_raw in enumerate(services_raw):
        try:
            services.append(
                _converter.structure(service_def_raw, config_models.ServiceDefinition)
            )
        except cattrs.ClassValidationError as exception:
            errors = "\n  ".join(cattrs.transform_error(exception))
            raise config_models.ConfigurationError(
                f"Invalid configuration for service at index {i}:\n  {errors}"
            ) from exception
    return services


def merge_handlers_dependencies_into_groups(
    actions: dict[str, config_models.ActionDefinition],
    deps_groups: dict[str, list[Any]],
) -> None:
    for action in actions.values():
        for handler in action.handlers:
            if not handler.env:
                continue
            if handler.env not in deps_groups:
                deps_groups[handler.env] = []
            deps_groups[handler.env] += handler.dependencies


def merge_services_dependencies_into_groups(
    services: list[config_models.ServiceDefinition],
    deps_groups: dict[str, list[Any]],
) -> None:
    for service in services:
        if service.env not in deps_groups:
            deps_groups[service.env] = []
        deps_groups[service.env] += service.dependencies


def _deduplicate_deps_groups(deps_groups: dict[str, list[Any]]) -> None:
    # dependency list can contain not only strings, but also dicts like
    # `{ 'include-group': 'runtime' }` which are not hashable, so use list-based dedup
    for group_name in deps_groups.keys():
        unique_deps: list[Any] = []
        for dep in deps_groups[group_name]:
            if dep not in unique_deps:
                unique_deps.append(dep)
        deps_groups[group_name] = unique_deps


def resolve_workspace_editable_packages(
    ws_context: context.WorkspaceContext,
) -> dict[str, Path]:
    """Resolve workspace editable packages from finecode-workspace.toml.

    Returns the union of:
      - Every discovered project's [project].name → project directory,
        when [workspace].all_workspace_packages_editable is True.
      - Each [workspace].editable_packages entry, validated.
    """
    if not ws_context.ws_dirs_paths:
        return {}

    ws_root = ws_context.ws_dirs_paths[0]
    ws_config_path = ws_root / "finecode-workspace.toml"
    if not ws_config_path.exists():
        return {}

    with open(ws_config_path, "rb") as f:
        ws_config = toml_loads(f.read()).unwrap()

    workspace_table = ws_config.get("workspace", {})
    all_editable: bool = workspace_table.get("all_workspace_packages_editable", False)
    explicit_paths: list[str] = workspace_table.get("editable_packages", [])

    result: dict[str, Path] = {}

    if all_editable:
        for project in ws_context.ws_projects.values():
            if project.name is None:
                continue
            result[project.name] = project.dir_path

    for raw_entry in explicit_paths:
        entry_path = Path(raw_entry)
        if not entry_path.is_absolute():
            entry_path = (ws_root / entry_path).resolve()
        if not entry_path.exists():
            raise config_models.ConfigurationError(
                f"[workspace].editable_packages entry '{raw_entry}' does not exist: {entry_path}"
            )
        pyproject_path = entry_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise config_models.ConfigurationError(
                f"[workspace].editable_packages entry '{raw_entry}' has no pyproject.toml: {entry_path}"
            )
        with open(pyproject_path, "rb") as f:
            entry_toml = toml_loads(f.read()).unwrap()
        pkg_name = entry_toml.get("project", {}).get("name")
        if pkg_name is None:
            raise config_models.ConfigurationError(
                f"[workspace].editable_packages entry '{raw_entry}' has no [project].name: {entry_path}"
            )
        if pkg_name in result:
            if result[pkg_name] != entry_path:
                raise config_models.ConfigurationError(
                    f"[workspace].editable_packages: package '{pkg_name}' resolves to two different paths: "
                    f"'{result[pkg_name]}' and '{entry_path}'"
                )
            # same name + same path → silent de-dup
        else:
            result[pkg_name] = entry_path

    return result


def add_extension_runner_to_dependencies(project_config: dict[str, Any]) -> None:
    try:
        deps_groups = project_config["dependency-groups"]
    except KeyError:
        return

    try:
        finecode_version = metadata.version("finecode")
    except metadata.PackageNotFoundError:
        # In editable/source-run setups package metadata for "finecode" may be
        # unavailable (e.g. uv + python -m finecode). Fall back to source version.
        try:
            from finecode._version import version as finecode_version
        except Exception:
            # TODO: raise an error?
            logger.warning(
                "Could not resolve finecode version from package metadata or source; "
                "skip automatic finecode_extension_runner pin injection"
            )
            return

    for group_name, group_packages in deps_groups.items():
        if group_name == "dev_workspace" or group_name == "runtime":
            # - skip `dev_workspace` because it contains finecode already
            # - skip `runtime` because FineCode doesn't start runner in runtime env, all
            # development-related processes happen in `dev` env.
            continue

        group_packages.append(f"finecode_extension_runner == {finecode_version}")
