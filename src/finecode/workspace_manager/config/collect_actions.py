from pathlib import Path
from typing import Any

import finecode.workspace_manager.config.config_models as config_models
import finecode.workspace_manager.context as context
import finecode.workspace_manager.domain as domain


def collect_actions(
    project_path: Path,
    ws_context: context.WorkspaceContext,
) -> list[domain.Action]:
    # preconditions:
    # - project raw config exists in ws_context if such project exists
    # - project expected to include finecode
    try:
        project = ws_context.ws_projects[project_path]
    except KeyError:
        raise ValueError(
            f"Project {project_path} doesn't exist."
            f" Existing projects: {ws_context.ws_projects}"
        )

    try:
        config = ws_context.ws_projects_raw_configs[project_path]
    except KeyError:
        raise Exception("First you need to parse config of project")

    actions = _collect_actions_in_config(config)
    project.actions = actions

    return actions


def _collect_actions_in_config(
    config: dict[str, Any],
) -> list[domain.Action]:
    actions: list[domain.Action] = []

    for action_name, action_def_raw in (
        config["tool"]["finecode"].get("action", {}).items()
    ):
        # TODO: handle validation errors
        action_def = config_models.ActionDefinition(**action_def_raw)
        new_action = domain.Action(
            name=action_name,
            handlers=[
                domain.ActionHandler(
                    name=handler.name,
                    source=handler.source,
                    config=config["tool"]["finecode"]
                    .get("action_handler", {})
                    .get(handler.name, {})
                    .get("config", {}),
                )
                for handler in action_def.handlers
            ],
            source=action_def.source,
            config=action_def.config or {},
        )
        actions.append(new_action)

    return actions
