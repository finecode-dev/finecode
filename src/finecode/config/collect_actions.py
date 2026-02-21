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
    except KeyError as exception:
        raise ValueError(
            f"Project {project_path} doesn't exist."
            + f" Existing projects: {ws_context.ws_projects}"
        ) from exception

    try:
        config = ws_context.ws_projects_raw_configs[project_path]
    except KeyError as exception:
        raise Exception("First you need to parse config of project") from exception

    actions = _collect_actions_in_config(config)
    project.actions = actions

    action_handler_configs = _collect_action_handler_configs_in_config(config)

    # Apply overrides
    #
    # Merge handler config overrides from ws_context if available
    if ws_context.handler_config_overrides:
        for action in project.actions:
            action_overrides = ws_context.handler_config_overrides.get(action.name, {})
            if not action_overrides:
                continue

            for handler in action.handlers:
                # Check for action-level overrides (empty string key)
                action_level_overrides = action_overrides.get("", {})
                # Check for handler-specific overrides
                handler_overrides = action_overrides.get(handler.name, {})

                # Merge overrides if any exist
                if action_level_overrides or handler_overrides:
                    if handler.source not in action_handler_configs:
                        action_handler_configs[handler.source] = {}
                    # Action-level first, then handler-specific (handler takes precedence)
                    action_handler_configs[handler.source] = {
                        **action_handler_configs[handler.source],
                        **action_level_overrides,
                        **handler_overrides,
                    }

    project.action_handler_configs = action_handler_configs

    return actions


def collect_services(
    project_path: Path,
    ws_context: context.WorkspaceContext,
) -> list[domain.ServiceDeclaration]:
    try:
        project = ws_context.ws_projects[project_path]
    except KeyError as exception:
        raise ValueError(
            f"Project {project_path} doesn't exist."
            + f" Existing projects: {ws_context.ws_projects}"
        ) from exception

    try:
        config = ws_context.ws_projects_raw_configs[project_path]
    except KeyError as exception:
        raise Exception("First you need to parse config of project") from exception

    services = _collect_services_in_config(config)
    project.services = services
    return services


def _collect_services_in_config(
    config: dict[str, Any],
) -> list[domain.ServiceDeclaration]:
    services: list[domain.ServiceDeclaration] = []
    for service_def_raw in config["tool"]["finecode"].get("service", []):
        try:
            service_def = config_models.ServiceDefinition(**service_def_raw)
        except config_models.ValidationError as exception:
            raise config_models.ConfigurationError(str(exception)) from exception

        services.append(
            domain.ServiceDeclaration(
                interface=service_def.interface,
                source=service_def.source,
                env=service_def.env,
                dependencies=service_def.dependencies,
            )
        )
    return services


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
            raise config_models.ConfigurationError(str(exception)) from exception

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
                if handler.enabled
            ],
            source=action_def.source,
            config=action_def.config or {},
        )
        actions.append(new_action)

    return actions
