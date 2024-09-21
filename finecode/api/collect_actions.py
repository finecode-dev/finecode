from pathlib import Path
from typing import Any

import finecode.config_models as config_models
import finecode.domain as domain
import finecode.workspace_context as workspace_context


def collect_actions_recursively(
    root_dir: Path, ws_context: workspace_context.WorkspaceContext
) -> domain.Package:
    try:
        root_package = ws_context.ws_packages[root_dir]
    except KeyError:
        raise Exception("Root package not found")

    return root_package

    # root_package = domain.Package(name=root_dir.name, path=root_dir)
    # def_files_generator = root_dir.rglob("*")
    # for def_file in def_files_generator:
    #     if def_file.name not in {"pyproject.toml", "package.json", "finecode.toml"}:
    #         continue

    #     if not finecode_is_enabled_in_def(def_file):
    #         continue

    #     path_parts = def_file.parent.relative_to(root_dir).parts
    #     current_package = root_package
    #     for part in path_parts:
    #         try:
    #             current_package = next(
    #                 package
    #                 for package in current_package.subpackages
    #                 if package.name == part
    #             )
    #         except StopIteration:
    #             new_package = domain.Package(
    #                 name=part, path=current_package.path / part
    #             )
    #             current_package.subpackages.append(new_package)
    #             current_package = new_package
    #
    #     root_actions, all_actions = collect_actions(def_file, ws_context=ws_context)
    #     for action_name in root_actions:
    #         try:
    #             action_info = all_actions[action_name]
    #             current_package.actions.append(action_info)
    #         except KeyError:
    #             # TODO: process correctly, return as invalid
    #             logger.warning(f"Action not found: {action_name}")
    # return root_package


def collect_actions(
    package_path: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> list[domain.Action]:
    # precondition: package raw config exists in ws_context if such package exists
    try:
        package = ws_context.ws_packages[package_path]
    except KeyError:
        raise ValueError(f"Package {package_path} doesn't exist. Existing packages: {ws_context.ws_packages}")

    if package.actions is not None:
        return package.actions

    try:
        config = ws_context.ws_packages_raw_configs[package_path]
    except KeyError:
        raise Exception("First you need to parse config of package")
    
    actions, actions_configs = _collect_actions_in_config(config)
    # TODO: validate
    first_level_actions_raw = [action_raw['name'] for action_raw in config["tool"]["finecode"].get("actions", [])]
    package.root_actions = first_level_actions_raw
    package.actions = actions
    package.actions_configs = actions_configs
    
    return actions


def _collect_actions_in_config(
    config: dict[str, Any]
) -> tuple[list[domain.Action], dict[str, dict[str, Any]]]:
    actions: list[domain.Action] = []
    actions_configs: dict[str, dict[str, Any]] = {}

    for action_name, action_def_raw in (
        config["tool"]["finecode"].get("action", {}).items()
    ):
        # TODO: handle validation errors
        action_def = config_models.ActionDefinition(**action_def_raw)
        actions.append(
            domain.Action(
                name=action_name,
                subactions=[subaction.name for subaction in action_def.subactions],
                source=action_def.source,
            )
        )
        if action_def.config is not None:
            actions_configs[action_name] = action_def.config

    return (actions, actions_configs)


def get_subaction(
    name: str, package_path: Path, ws_context: workspace_context.WorkspaceContext
) -> domain.Action:
    try:
        package_raw_config = ws_context.ws_packages_raw_configs[package_path]
    except KeyError:
        raise ValueError("Package config not found")

    try:
        action_raw = package_raw_config["tool"]["finecode"]["action"][name]
    except KeyError:
        raise ValueError("Action definition not found")
    try:
        return domain.Action(name=name, source=action_raw["source"])
    except ValueError:
        raise ValueError("Action has no source")
