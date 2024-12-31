from pathlib import Path
from typing import Any

import finecode.workspace_manager.config_models as config_models
import finecode.workspace_manager.context as context
import finecode.workspace_manager.domain as domain


def collect_actions(
    project_path: Path,
    ws_context: context.WorkspaceContext,
) -> list[domain.Action]:
    # precondition: project raw config exists in ws_context if such project exists
    try:
        project = ws_context.ws_projects[project_path]
    except KeyError:
        raise ValueError(
            f"Project {project_path} doesn't exist. Existing projects: {ws_context.ws_projects}"
        )

    if project.actions is not None:
        return project.actions

    try:
        config = ws_context.ws_projects_raw_configs[project_path]
    except KeyError:
        raise Exception("First you need to parse config of project")

    if config.get("tool", {}).get("finecode", None) is None:
        project.status = domain.ProjectStatus.NO_FINECODE
        project.actions = []
        project.actions_configs = {}
        return []

    actions, actions_configs = _collect_actions_in_config(config)
    # TODO: validate
    first_level_actions_raw = [
        action_raw["name"] for action_raw in config["tool"]["finecode"].get("actions", [])
    ]
    project.root_actions = first_level_actions_raw
    project.actions = actions
    project.actions_configs = actions_configs

    return actions


def _collect_actions_in_config(
    config: dict[str, Any],
) -> tuple[list[domain.Action], dict[str, dict[str, Any]]]:
    actions: list[domain.Action] = []
    actions_configs: dict[str, dict[str, Any]] = {}

    for action_name, action_def_raw in config["tool"]["finecode"].get("action", {}).items():
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
