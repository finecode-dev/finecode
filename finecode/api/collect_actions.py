import os
from pathlib import Path
from typing import NamedTuple

from command_runner import command_runner
from loguru import logger
from pydantic import BaseModel
from tomlkit import loads as toml_loads

import finecode.domain as domain
import finecode.workspace_context as workspace_context


def finecode_is_enabled_in_def(def_file: Path) -> bool:
    if def_file.name == "finecode.toml":
        return True

    if def_file.name == "pyproject.toml":
        with open(def_file, "rb") as pyproject_file:
            project_def = toml_loads(pyproject_file.read())
        return project_def.get("tool", {}).get("finecode", None) is not None

    return False


def collect_actions_recursively(
    root_dir: Path, ws_context: workspace_context.WorkspaceContext
) -> domain.Package:
    root_package = domain.Package(name=root_dir.name, path=root_dir)
    def_files_generator = root_dir.rglob("*")
    for def_file in def_files_generator:
        if def_file.name not in {"pyproject.toml", "package.json", "finecode.toml"}:
            continue

        if not finecode_is_enabled_in_def(def_file):
            continue

        path_parts = def_file.parent.relative_to(root_dir).parts
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

        root_actions, all_actions = collect_actions(def_file, ws_context=ws_context)
        for action_name in root_actions:
            try:
                action_info = all_actions[action_name]
                current_package.actions.append(action_info)
            except KeyError:
                # TODO: process correctly, return as invalid
                logger.warning(f"Action not found: {action_name}")
    return root_package


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


def collect_actions(
    project_def_path: Path, ws_context: workspace_context.WorkspaceContext
) -> tuple[domain.RootActions, domain.AllActions]:
    if project_def_path.as_posix() in ws_context.actions_by_package_path:
        logger.trace(f"Found actions for {project_def_path.as_posix()} in context")
        return ws_context.actions_by_package_path[project_def_path.as_posix()]

    if project_def_path.name == "pyproject.toml":
        result = collect_actions_pyproject(project_def_path, ws_context=ws_context)
    else:
        result = ([], {})

    ws_context.actions_by_package_path[project_def_path.as_posix()] = result
    return result


class Preset(BaseModel):
    source: str


class Action(BaseModel):
    name: str
    # TODO: validate that either source or subactions are required
    source: str | None = None
    subactions: list[str] = []


class FinecodeConfig(BaseModel):
    presets: list[Preset] = []
    actions: list[Action] = []


class PresetConfig(BaseModel):
    extends: list[Preset] = []
    actions: list[Action] = []


class ActionConfig(BaseModel):
    # TODO: validate that one of both is required
    source: str | None = None
    subactions: list[Action] = []


def collect_actions_pyproject(
    pyproject_path: Path, ws_context: workspace_context.WorkspaceContext
) -> tuple[domain.RootActions, domain.AllActions]:
    # use root pyproject.toml as base (TODO: differ workspace and project in future?)
    if not pyproject_path.exists():
        raise Exception(
            f"No pyproject.toml found in {pyproject_path.parent}"
        )  # TODO: improve

    with open(pyproject_path, "rb") as pyproject_file:
        project_def = toml_loads(pyproject_file.read())

    root_actions: domain.RootActions = []
    all_actions: domain.AllActions = {}
    try:
        finecode_config = FinecodeConfig(**project_def["tool"]["finecode"])
    # TODO: handle validation error
    except KeyError:
        return (root_actions, all_actions)

    root_actions, all_actions = collect_actions_from_py_presets(
        presets=finecode_config.presets, def_path=pyproject_path
    )

    for action_name, action_def_raw in (
        project_def["tool"]["finecode"].get("action", {}).items()
    ):
        # TODO: handle validation errors
        action_def = ActionConfig(**action_def_raw)
        subactions: list[str] = []
        for subaction in action_def.subactions:
            subactions.append(subaction.name)
            if subaction.source is not None:
                all_actions[subaction.name] = domain.Action(
                    name=subaction.name, source=subaction.source
                )
        all_actions[action_name] = domain.Action(
            name=action_name, subactions=subactions, source=action_def.source
        )

    for root_action in finecode_config.actions:
        root_actions.append(root_action.name)
        if root_action.source is not None:
            source = root_action.source
            subactions = []
            all_actions[root_action.name] = domain.Action(
                name=root_action.name, subactions=subactions, source=source
            )
        else:
            if root_action.name not in all_actions:
                raise Exception(
                    f"Action {root_action.name} has neither source or definition with subactions"
                )

    return (root_actions, all_actions)


class PresetToProcess(NamedTuple):
    source: str
    package_def_path: Path


def collect_actions_from_py_presets(
    presets: list[Preset], def_path: Path
) -> tuple[domain.RootActions, domain.AllActions]:
    # actions: list[domain.Action] = []
    root_actions: domain.RootActions = []
    all_actions: domain.AllActions = {}
    processed_presets: set[str] = set()
    presets_to_process: set[PresetToProcess] = set(
        [
            PresetToProcess(source=preset.source, package_def_path=def_path)
            for preset in presets
        ]
    )
    while len(presets_to_process) > 0:
        preset = presets_to_process.pop()
        processed_presets.add(preset.source)

        old_current_dir = os.getcwd()
        os.chdir(def_path.parent)  # preset.package_def_path.parent
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
            preset_toml = toml_loads(preset_toml_file.read())

        try:
            preset_config = PresetConfig(**preset_toml["finecode"]["preset"])
        except KeyError:  # TODO: handle validation errors
            logger.trace(f"Preset {preset} has no config yet")
            continue

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

        for action in preset_config.actions:
            if action.source is not None:
                all_actions[action.name] = domain.Action(
                    name=action.name, source=action.source
                )
                root_actions.append(action.name)
            else:
                try:
                    # TODO: handle validation errors
                    action_config = ActionConfig(
                        **preset_toml["tool"]["finecode"]["action"][action.name]
                    )
                    root_actions.append(action.name)
                    all_actions[action.name] = domain.Action(
                        name=action.name,
                        subactions=[
                            subaction.name for subaction in action_config.subactions
                        ],
                    )
                    for subaction in action_config.subactions:
                        all_actions[subaction.name] = domain.Action(
                            name=subaction.name, source=subaction.source
                        )
                except KeyError:
                    logger.error(f"Action definition not found {action}")

    return (root_actions, all_actions)
