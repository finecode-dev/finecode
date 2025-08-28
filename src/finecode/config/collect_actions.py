from pathlib import Path
from typing import Any

import finecode.config.config_models as config_models
import finecode.context as context
import finecode.domain as domain


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

    action_handler_configs = _collect_action_handler_configs_in_config(config)
    project.action_handler_configs = action_handler_configs

    return actions


def _collect_action_handler_configs_in_config(
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    action_handlers_configs = config["tool"]["finecode"].get("action_handler", [])
    action_handler_config_by_source: dict[str, dict[str, Any]] = {}
    for handler_def in action_handlers_configs:
        if "source" not in handler_def or not isinstance(handler_def["source"], str):
            raise config_models.ConfigurationError(
                "Action handler definition expected to have a 'source' field(to identify handler) and it should be a string"
            )

        handler_config = handler_def.get("config", None)
        if handler_config is not None:
            action_handler_config_by_source[handler_def["source"]] = handler_config

    return action_handler_config_by_source


def _collect_actions_in_config(
    config: dict[str, Any],
) -> list[domain.Action]:
    actions: list[domain.Action] = []
    for action_name, action_def_raw in (
        config["tool"]["finecode"].get("action", {}).items()
    ):

        try:
            action_def = config_models.ActionDefinition(**action_def_raw)
        except config_models.ValidationError as exception:
            raise config_models.ConfigurationError(str(exception))

        new_action = domain.Action(
            name=action_name,
            handlers=[
                domain.ActionHandler(
                    name=handler.name,
                    source=handler.source,
                    config=handler.config or {},
                    env=handler.env,
                    dependencies=handler.dependencies,
                )
                for handler in action_def.handlers
            ],
            source=action_def.source,
            config=action_def.config or {},
        )
        actions.append(new_action)

    return actions
