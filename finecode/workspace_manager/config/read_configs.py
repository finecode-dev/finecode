from pathlib import Path
from typing import Any, NamedTuple

from loguru import logger
from tomlkit import loads as toml_loads

from finecode.workspace_manager import context, domain, user_messages
from finecode.workspace_manager.config import config_models
from finecode.workspace_manager.runner import runner_client, runner_info


async def read_projects_in_dir(
    dir_path: Path, ws_context: context.WorkspaceContext
) -> list[domain.Project]:
    # Find all projects in directory
    # `dir_path` expected to be absolute path
    logger.trace(f"Read directories in {dir_path}")
    new_projects: list[domain.Project] = []
    def_files_generator = dir_path.rglob("pyproject.toml")
    for def_file in def_files_generator:
        status = domain.ProjectStatus.READY
        actions: list[domain.Action] | None = None

        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read()).value

        if project_def.get("tool", {}).get("finecode", None) is None:
            status = domain.ProjectStatus.NO_FINECODE
            actions = []
        else:
            # finecode config exists, check also finecode.sh
            finecode_sh_path = def_file.parent / "finecode.sh"

            if not finecode_sh_path.exists():
                status = domain.ProjectStatus.NO_FINECODE_SH

        new_project = domain.Project(
            name=def_file.parent.name,
            dir_path=def_file.parent,
            def_path=def_file,
            status=status,
            actions=actions,
        )
        ws_context.ws_projects[def_file.parent] = new_project
        new_projects.append(new_project)
    return new_projects


async def read_project_config(
    project: domain.Project, ws_context: context.WorkspaceContext
) -> None:
    # this function requires running project extension runner to get configuration
    # from it
    if project.def_path.name == "pyproject.toml":
        with open(project.def_path, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read()).value
        # TODO: validate that finecode is installed?

        finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
        if finecode_raw_config:
            finecode_config = config_models.FinecodeConfig(**finecode_raw_config)
            new_config = await collect_config_from_py_presets(
                presets_sources=[preset.source for preset in finecode_config.presets],
                def_path=project.def_path,
                runner=ws_context.ws_projects_extension_runners[project.dir_path],
            )
            _merge_projects_configs(project_def, new_config)

        ws_context.ws_projects_raw_configs[project.dir_path] = project_def
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
) -> tuple[dict[str, Any] | None, config_models.PresetDefinition | None]:
    # preset_id is used only for logs to make them more useful
    logger.trace(f"Read preset config: {preset_id}")
    if not config_path.exists():
        logger.error(f"preset.toml not found in project '{preset_id}'")
        return (None, None)

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
) -> dict[str, Any]:
    config: dict[str, Any] = {}
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
            continue

        preset_toml_path = preset_project_path / "preset.toml"
        preset_toml, preset_config = read_preset_config(preset_toml_path, preset.source)
        if preset_toml is None or preset_config is None:
            continue

        _merge_preset_configs(config, preset_toml)
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


def _merge_projects_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 without overwriting
    if "tool" not in config1:
        config1["tool"] = {}
    if "finecode" not in config1["tool"]:
        config1["tool"]["finecode"] = {}

    tool_finecode_config1 = config1["tool"]["finecode"]
    tool_finecode_config2 = config2.get("tool", {}).get("finecode", {})

    for key, value in tool_finecode_config2.items():
        if key == "action" or key == "action_handler":
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
        elif key in config1:
            tool_finecode_config1[key].update(value)
        else:
            tool_finecode_config1[key] = value


def _merge_preset_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 (in-place)
    # config1 is not overwritten by config2
    new_views = config2.get("tool", {}).get("finecode", {}).get("views", None)
    new_actions_defs_and_configs = (
        config2.get("tool", {}).get("finecode", {}).get("action", None)
    )
    new_actions_handlers_configs = (
        config2.get("tool", {}).get("finecode", {}).get("action_handler", None)
    )
    if (
        new_views is not None
        or new_actions_defs_and_configs is not None
        or new_actions_handlers_configs is not None
    ):
        if "tool" not in config1:
            config1["tool"] = {}
        if "finecode" not in config1["tool"]:
            config1["tool"]["finecode"] = {}

        if new_views is not None:
            if "views" not in config1["tool"]["finecode"]:
                config1["tool"]["finecode"]["views"] = []
            config1["tool"]["finecode"]["views"].extend(new_views)
            del config2["tool"]["finecode"]["views"]

        if new_actions_defs_and_configs is not None:
            if "action" not in config1["tool"]["finecode"]:
                config1["tool"]["finecode"]["action"] = {}

            for handler_name, handler_info in new_actions_defs_and_configs.items():
                if handler_name not in config1["tool"]["finecode"]["action"]:
                    config1["tool"]["finecode"]["action"][handler_name] = {}

                action_def = {
                    key: value for key, value in handler_info.items() if key != "config"
                }
                config1["tool"]["finecode"]["action"][handler_name].update(action_def)

                try:
                    handler_config = handler_info["config"]
                except KeyError:
                    continue

                handler_config.update(
                    config1["tool"]["finecode"]["action"][handler_name].get(
                        "config", {}
                    )
                )
                config1["tool"]["finecode"]["action"][handler_name][
                    "config"
                ] = handler_config

            del config2["tool"]["finecode"]["action"]

    if new_actions_handlers_configs is not None:
        if "action_handler" not in config1["tool"]["finecode"]:
            config1["tool"]["finecode"]["action_handler"] = {}

        for handler_name, handler_info in new_actions_handlers_configs.items():
            if handler_name not in config1["tool"]["finecode"]["action_handler"]:
                config1["tool"]["finecode"]["action_handler"][handler_name] = {}

            try:
                handler_config = handler_info["config"]
            except KeyError:
                continue

            handler_config.update(
                config1["tool"]["finecode"]["action_handler"][handler_name].get(
                    "config", {}
                )
            )
            config1["tool"]["finecode"]["action_handler"][handler_name][
                "config"
            ] = handler_config

        del config2["tool"]["finecode"]["action_handler"]

    del config2["tool"]["finecode"]
