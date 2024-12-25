import os
from pathlib import Path
from typing import Any, NamedTuple

from command_runner import command_runner
from loguru import logger
from pydantic import ValidationError
from tomlkit import loads as toml_loads

from finecode.workspace_manager import config_models, context, domain


def read_configs(ws_context: context.WorkspaceContext):
    # Read configs in all root directories of workspace
    logger.trace("Read configs in workspace")
    for ws_dir_path in ws_context.ws_dirs_paths:
        read_configs_in_dir(dir_path=ws_dir_path, ws_context=ws_context)
    logger.trace("Reading configs in workspace finished")


def read_configs_in_dir(dir_path: Path, ws_context: context.WorkspaceContext) -> None:
    # Find all projects, read their configs and save in ws context. Resolve presets and all 'source'
    # properties
    logger.trace(f"Read configs in {dir_path}")
    def_files_generator = dir_path.rglob("*")
    for def_file in def_files_generator:
        if def_file.name not in {
            "pyproject.toml",
        }:  # "package.json", "finecode.toml"
            continue

        if def_file.name == "pyproject.toml":
            with open(def_file, "rb") as pyproject_file:
                project_def = toml_loads(pyproject_file.read()).value
            # TODO: validate that finecode is installed?

            finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
            if finecode_raw_config:
                finecode_config = config_models.FinecodeConfig(**finecode_raw_config)
                new_config = collect_config_from_py_presets(
                    presets_sources=[preset.source for preset in finecode_config.presets],
                    def_path=def_file,
                )
                _merge_projects_configs(project_def, new_config)

            normalize_project_config(project_def)
            ws_context.ws_projects_raw_configs[def_file.parent] = project_def
        else:
            logger.info(f"Project definition of type {def_file.name} is not supported yet")
            continue

        finecode_sh_path = def_file.parent / "finecode.sh"
        status = domain.ProjectStatus.READY
        if not finecode_sh_path.exists():
            status = domain.ProjectStatus.NO_FINECODE_SH

        ws_context.ws_projects[def_file.parent] = domain.Project(
            name=def_file.parent.name, path=def_file.parent, status=status
        )


def normalize_project_config(config: dict[str, Any]) -> None:
    # normalizes config in-place
    actions_dict = config.get("tool", {}).get("finecode", {}).get("action", None)
    if actions_dict is not None:
        # copy dict values to avoid changing size of iterated list during iteration
        for action in [*actions_dict.values()]:
            for subaction in action.get("subactions", []):
                # each action should be declared as 'tool.finecode.action.<name>', but we allow
                # to set source directly in list of subactions to improve usability
                if isinstance(subaction, dict) and subaction.get("source", None) is not None:
                    config["tool"]["finecode"]["action"][subaction["name"]] = {
                        "source": subaction["source"],
                        # avoid overwriting existing properties, e.g. action config
                        **config["tool"]["finecode"]["action"].get(subaction["name"], {}),
                    }


class PresetToProcess(NamedTuple):
    source: str
    project_def_path: Path


def get_preset_project_path(preset: PresetToProcess, def_path: Path) -> Path | None:
    logger.trace(f"Get preset project path: {preset.source}")
    old_current_dir = os.getcwd()
    os.chdir(def_path.parent)
    exit_code, output = command_runner(
        f'poetry run python -c "import {preset.source}; import os;'
        f' print(os.path.dirname({preset.source}.__file__))"'
    )
    os.chdir(old_current_dir)
    if exit_code != 0 or not isinstance(output, str):
        logger.error(f"Cannot resolve preset {preset.source}")
        return None

    preset_project_path = Path(output.strip("\n"))
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
        preset_config = config_models.PresetDefinition(**preset_toml["tool"]["finecode"]["preset"])
    except ValidationError as e:
        logger.error(str(preset_toml["tool"]["finecode"]["preset"]) + e.json())
        return (preset_toml, None)
    except KeyError:
        logger.trace(f"Preset {preset_id} has no config yet")
        return (preset_toml, None)

    # extends is used only to get "parent" presets and is not part of public config
    if "extends" in preset_toml:
        del preset_toml["extends"]

    logger.trace(f"Reading preset config finished: {preset_id}")
    return (preset_toml, preset_config)


def collect_config_from_py_presets(presets_sources: list[str], def_path: Path) -> dict[str, Any]:
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

        preset_project_path = get_preset_project_path(preset=preset, def_path=def_path)
        if preset_project_path is None:
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

    return _preset_config_to_project_config(config)


def optimize_project_tree(root_project: domain.Project) -> domain.Project:
    """
    Combine empty projects:
    - project1
    -- project2
    --- action1
    ->
    - project1/project2
    -- action1

    Root project is not optimized.
    """
    # TODO
    ...


def _finecode_is_enabled_in_def(def_file: Path) -> bool:
    if def_file.name == "finecode.toml":
        return True

    if def_file.name == "pyproject.toml":
        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read())
        return project_def.get("tool", {}).get("finecode", None) is not None

    return False


def _merge_projects_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 without overwriting
    if not "tool" in config1:
        config1["tool"] = {}
    if not "finecode" in config1["tool"]:
        config1["tool"]["finecode"] = {}

    tool_finecode_config1 = config1["tool"]["finecode"]
    tool_finecode_config2 = config2.get("tool", {}).get("finecode", {})

    for key, value in tool_finecode_config2.items():
        if key == "action":
            # first process actions explicitly to merge correct configs
            assert isinstance(value, dict)
            if not "action" in tool_finecode_config1:
                tool_finecode_config1["action"] = {}
            for action_name, action_info in value.items():
                if action_name not in tool_finecode_config1["action"]:
                    # new action, just add as it is
                    tool_finecode_config1["action"][action_name] = action_info
                else:
                    # action with the same name, merge
                    if "config" in action_info:
                        new_action_config = action_info.get("config", {}).update(
                            tool_finecode_config1["action"][action_name].get("config", {})
                        )
                        tool_finecode_config1["action"][action_name]["config"] = new_action_config
                    new_action_info = action_info.update(
                        tool_finecode_config1["action"][action_name]
                    )

                    tool_finecode_config1["action"][action_name] = new_action_info
        elif key in config1:
            tool_finecode_config1[key].update(value)
        else:
            tool_finecode_config1[key] = value


def _merge_preset_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 (in-place)
    # config1 is not overwritten by config2
    new_actions = config2.get("tool", {}).get("finecode", {}).get("preset", {}).get("actions", None)
    new_views = config2.get("tool", {}).get("finecode", {}).get("preset", {}).get("views", None)
    new_actions_defs_and_configs = config2.get("tool", {}).get("finecode", {}).get("action", None)
    if new_actions is not None or new_views is not None or new_actions_defs_and_configs is not None:
        if not "tool" in config1:
            config1["tool"] = {}
        if not "finecode" in config1["tool"]:
            config1["tool"]["finecode"] = {}
        if not "preset" in config1["tool"]["finecode"]:
            config1["tool"]["finecode"]["preset"] = {}

        if new_actions is not None:
            if not "actions" in config1["tool"]["finecode"]["preset"]:
                config1["tool"]["finecode"]["preset"]["actions"] = []
            config1["tool"]["finecode"]["preset"]["actions"].extend(new_actions)
            del config2["tool"]["finecode"]["preset"]["actions"]

        if new_views is not None:
            if not "views" in config1["tool"]["finecode"]["preset"]:
                config1["tool"]["finecode"]["preset"]["views"] = []
            config1["tool"]["finecode"]["preset"]["views"].extend(new_views)
            del config2["tool"]["finecode"]["preset"]["views"]

        if new_actions_defs_and_configs is not None:
            if not "action" in config1["tool"]["finecode"]["preset"]:
                config1["tool"]["finecode"]["preset"]["action"] = {}

            for action_name, action_info in new_actions_defs_and_configs.items():
                if action_name not in config1["tool"]["finecode"]["preset"]["action"]:
                    config1["tool"]["finecode"]["preset"]["action"][action_name] = {}

                action_def = {key: value for key, value in action_info.items() if key != 'config'}
                config1["tool"]["finecode"]["preset"]["action"][action_name].update(action_def)

                try:
                    action_config = action_info["config"]
                except KeyError:
                    continue

                action_config.update(
                    config1["tool"]["finecode"]["preset"]["action"][action_name].get('config', {})
                )
                config1["tool"]["finecode"]["preset"]["action"][action_name]["config"] = action_config

            del config2["tool"]["finecode"]["action"]

        del config2["tool"]["finecode"]["preset"]
        del config2["tool"]["finecode"]


def _preset_config_to_project_config(preset_config: dict[str, Any]) -> dict[str, Any]:
    # tool.finecode.preset -> tool.finecode
    result = preset_config.copy()
    if preset_config.get("tool", {}).get("finecode", {}).get("preset", None) is not None:
        if not "tool" in result:
            result["tool"] = {}
        if not "finecode" in result["tool"]:
            result["tool"]["finecode"] = {}

        result["tool"]["finecode"].update(preset_config["tool"]["finecode"]["preset"])
        del result["tool"]["finecode"]["preset"]

    return result
