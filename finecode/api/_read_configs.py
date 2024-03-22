import os
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError
from tomlkit import loads as toml_loads
from command_runner import command_runner
from loguru import logger

from finecode import workspace_context, domain, config_models


def read_configs(ws_context: workspace_context.WorkspaceContext):
    # Read configs in all root directories of workspace
    logger.trace("Read configs in workspace")  # TODO: ws id?
    for ws_dir_path in ws_context.ws_dirs_pathes:
        read_configs_in_dir(dir_path=ws_dir_path, ws_context=ws_context)
    logger.trace("Reading configs in workspace finished")


def read_configs_in_dir(
    dir_path: Path, ws_context: workspace_context.WorkspaceContext
) -> None:
    # Find all packages, read their configs and save in ws context. Resolve presets and all 'source'
    # properties
    logger.trace(f"Read configs in {dir_path}")
    root_package = domain.Package(name=dir_path.name, path=dir_path)
    def_files_generator = dir_path.rglob("*")
    for def_file in def_files_generator:
        if def_file.name not in {
            "pyproject.toml",
        }:  # "package.json", "finecode.toml"
            continue

        if def_file.name == "pyproject.toml":
            with open(def_file, "rb") as pyproject_file:
                project_def = toml_loads(pyproject_file.read()).value

            finecode_raw_config = project_def.get("tool", {}).get("finecode", None)
            if finecode_raw_config:
                finecode_config = config_models.FinecodeConfig(**finecode_raw_config)
                new_config = collect_config_from_py_presets(
                    presets_sources=[
                        preset.source for preset in finecode_config.presets
                    ],
                    def_path=def_file,
                )
                _merge_package_configs(project_def, new_config)

            normalize_package_config(project_def)
            ws_context.ws_packages_raw_configs[def_file.parent] = project_def

        path_parts = def_file.parent.relative_to(dir_path).parts
        current_package = root_package
        for part in path_parts:
            try:
                current_package = next(
                    package
                    for package in current_package.subpackages
                    if package.name == part
                )
            except StopIteration:
                new_package = domain.Package(
                    name=part, path=current_package.path / part
                )
                current_package.subpackages.append(new_package)
                current_package = new_package

    ws_context.ws_packages[dir_path] = root_package


def normalize_package_config(config: dict[str, Any]) -> None:
    # normalizes config in-place
    actions_dict = config.get("tool", {}).get("finecode", {}).get("action", None)
    if actions_dict is not None:
        # copy dict values to avoid changing size of iterated list during iteration
        for action in [*actions_dict.values()]:
            for subaction in action.get("subactions", []):
                # each action should be declared as 'tool.finecode.action.<name>', but we allow
                # to set source directly in list of subactions to improve usability
                if (
                    isinstance(subaction, dict)
                    and subaction.get("source", None) is not None
                ):
                    config["tool"]["finecode"]["action"][subaction["name"]] = {
                        "source": subaction["source"],
                        # avoid overwriting existing properties, e.g. action config
                        **config["tool"]["finecode"]["action"].get(subaction["name"], {})
                    }


class PresetToProcess(NamedTuple):
    source: str
    package_def_path: Path


def get_preset_package_path(preset: PresetToProcess, def_path: Path) -> Path | None:
    logger.trace(f"Get preset package path: {preset.source}")
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

    preset_package_path = Path(output.strip("\n"))
    logger.trace(f"Got: {preset.source} -> {preset_package_path}")
    return preset_package_path


def read_preset_config(
    config_path: Path, preset_id: str
) -> tuple[dict[str, Any] | None, config_models.PresetDefinition | None]:
    # preset_id is used only for logs to make them more useful
    logger.trace(f"Read preset config: {preset_id}")
    if not config_path.exists():
        logger.error(f"preset.toml not found in package '{preset_id}'")
        return (None, None)

    with open(config_path, "rb") as preset_toml_file:
        preset_toml = toml_loads(preset_toml_file.read()).value

    try:
        preset_config = config_models.PresetDefinition(**preset_toml["finecode"]["preset"])
    except ValidationError as e:
        logger.error(str(preset_toml["finecode"]["preset"]) + e.json())
        return (preset_toml, None)
    except KeyError:
        logger.trace(f"Preset {preset_id} has no config yet")
        return (preset_toml, None)

    # extends is used only to get "parent" presets and is not part of public config
    if "extends" in preset_toml:
        del preset_toml["extends"]

    logger.trace(f"Reading preset config finished: {preset_id}")
    return (preset_toml, preset_config)


def collect_config_from_py_presets(
    presets_sources: list[str], def_path: Path
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    processed_presets: set[str] = set()
    presets_to_process: set[PresetToProcess] = set(
        [
            PresetToProcess(source=preset_source, package_def_path=def_path)
            for preset_source in presets_sources
        ]
    )
    while len(presets_to_process) > 0:
        preset = presets_to_process.pop()
        processed_presets.add(preset.source)

        preset_package_path = get_preset_package_path(preset=preset, def_path=def_path)
        if preset_package_path is None:
            continue

        preset_toml_path = preset_package_path / "preset.toml"
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
                    package_def_path=def_path,
                )
            )

    return _preset_config_to_package_config(config)


def optimize_package_tree(root_package: domain.Package) -> domain.Package:
    """
    Combine empty packages:
    - package1
    -- package2
    --- action1
    ->
    - package1/package2
    -- action1

    Root package is not optimized.
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


def _merge_package_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
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
                            tool_finecode_config1["action"][action_name].get(
                                "config", {}
                            )
                        )
                        tool_finecode_config1["action"][action_name]['config'] = new_action_config
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
    new_actions = config2.get("finecode", {}).get("preset", {}).get("actions", None)
    new_views = config2.get("finecode", {}).get("preset", {}).get("views", None)
    new_actions_configs = config2.get("finecode", {}).get("action", None)
    if (
        new_actions is not None
        or new_views is not None
        or new_actions_configs is not None
    ):
        if not "finecode" in config1:
            config1["finecode"] = {}
        if not "preset" in config1["finecode"]:
            config1["finecode"]["preset"] = {}

        if new_actions is not None:
            if not "actions" in config1["finecode"]["preset"]:
                config1["finecode"]["preset"]["actions"] = []
            config1["finecode"]["preset"]["actions"].extend(new_actions)
            del config2["finecode"]["preset"]["actions"]

        if new_views is not None:
            if not "views" in config1["finecode"]["preset"]:
                config1["finecode"]["preset"]["views"] = []
            config1["finecode"]["preset"]["views"].extend(new_views)
            del config2["finecode"]["preset"]["views"]

        if new_actions_configs is not None:
            if not "action" in config1:
                config1["action"] = {}

            for action_name, action_info in new_actions_configs.items():
                try:
                    action_config = action_info["config"]
                except KeyError:
                    continue

                if action_name not in config1["finecode"]["action"]:
                    config1["finecode"]["action"][action_name] = {}
                new_action_config = action_config.update(
                    config1["finecode"]["action"][action_name].get("config", {})
                )
                config1["finecode"]["action"][action_name]["config"] = new_action_config

            del config2["finecode"]["action"]

        del config2["finecode"]["preset"]
        del config2["finecode"]

    config1.update(config2)


def _preset_config_to_package_config(preset_config: dict[str, Any]) -> dict[str, Any]:
    # finecode.preset -> tool.finecode
    result = preset_config.copy()
    if preset_config.get("finecode", {}).get("preset", None) is not None:
        if not "tool" in result:
            result["tool"] = {}
        if not "finecode" in result["tool"]:
            result["tool"]["finecode"] = {}
        result["tool"]["finecode"].update(preset_config["finecode"]["preset"])
        del result["finecode"]
    return result
