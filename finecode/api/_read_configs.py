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
    for ws_dir_path in ws_context.ws_dirs_pathes:
        read_configs_in_dir(dir_path=ws_dir_path, ws_context=ws_context)


def read_configs_in_dir(
    dir_path: Path, ws_context: workspace_context.WorkspaceContext
) -> None:
    # Find all packages, read their configs and save in ws context. Resolve presets and all 'source'
    # properties
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
            # TODO: resolve 'source' ?
            ws_context.ws_packages_raw_configs[
                def_file.parent
            ] = project_def  # TODO: normalize

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


class PresetToProcess(NamedTuple):
    source: str
    package_def_path: Path


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

        old_current_dir = os.getcwd()
        os.chdir(def_path.parent)
        exit_code, output = command_runner(
            f'poetry run python -c "import {preset.source}; import os;'
            f' print(os.path.dirname({preset.source}.__file__))"'
        )
        os.chdir(old_current_dir)
        if exit_code != 0 or not isinstance(output, str):
            logger.error(f"Cannot resolve preset {preset.source}")
            continue

        preset_package_path = Path(output.strip("\n"))
        preset_toml_path = preset_package_path / "preset.toml"
        if not preset_toml_path.exists():
            logger.error(f"preset.toml not found in package '{preset}'")
            continue

        with open(preset_toml_path, "rb") as preset_toml_file:
            preset_toml = toml_loads(preset_toml_file.read()).value

        try:
            preset_config = config_models.PresetConfig(
                **preset_toml["finecode"]["preset"]
            )
        except ValidationError as e:
            logger.error(str(preset_toml["finecode"]["preset"]) + e.json())
            continue
        except KeyError:  # TODO: handle validation errors
            logger.trace(f"Preset {preset} has no config yet")
            continue

        # extends is used only to get "parent" presets and is not part of public config
        if "extends" in preset_toml:
            del preset_toml["extends"]
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
    for key, value in config2.items():
        if key in config1:
            config1[key].update(value)
        else:
            config1[key] = value


def _merge_preset_configs(config1: dict[str, Any], config2: dict[str, Any]) -> None:
    # merge config2 in config1 (in-place)
    new_actions = config2.get("finecode", {}).get("preset", {}).get("actions", None)
    new_views = config2.get("finecode", {}).get("preset", {}).get("views", None)
    if new_actions is not None or new_views is not None:
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
