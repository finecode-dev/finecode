from pathlib import Path
from typing import Any

import finecode.config_models as config_models
import finecode.domain as domain
import finecode.workspace_context as workspace_context


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

    if config.get('tool', {}).get('finecode', None) is None:
        package.status = domain.PackageStatus.NO_FINECODE
        package.actions = []
        package.actions_configs = {}
        return []

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
    except KeyError:
        raise ValueError("Action has no source")
