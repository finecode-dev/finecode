# docs: docs/configuration.md
from pathlib import Path
from typing import Any

import cattrs

import finecode.wm_server.config.config_models as config_models
from finecode._converter import converter as _converter
from finecode.wm_server import context, domain
from finecode.wm_server.config.read_configs import read_env_configs


def collect_project(
    project_path: Path,
    ws_context: context.WorkspaceContext,
    presets_resolved: bool = True,
) -> domain.CollectedProject:
    """Collect actions, services, and handler configs from the project's raw config.

    Constructs a :class:`~finecode.wm_server.domain.CollectedProject` and
    replaces the existing entry in ``ws_context.ws_projects``.  The raw config
    must already be present (call ``read_project_config`` first).

    Args:
        presets_resolved: Whether preset configs have already been merged into the
            raw config.  When ``True``,
            an action without a ``source`` is a configuration error.  When ``False``
            (early collection before any runner is available), sourceless entries are
            silently skipped — they are config-only overrides whose source will arrive
            once presets are merged.
    """
    try:
        project = ws_context.ws_projects[project_path]
    except KeyError as exception:
        raise ValueError(
            f"collect_project called for unknown project {project_path}. "
            f"Known projects: {list(ws_context.ws_projects)}"
        ) from exception

    try:
        config = ws_context.ws_projects_raw_configs[project_path]
    except KeyError as exception:
        raise ValueError(
            f"collect_project called before raw config was read for {project_path}. "
            f"Call read_project_config first."
        ) from exception

    actions = _collect_actions_in_config(config, presets_resolved=presets_resolved)
    action_handler_configs = _collect_action_handler_configs_in_config(config)
    services = _collect_services_in_config(config)
    env_configs = read_env_configs(project_config=config)

    collected = domain.CollectedProject(
        name=project.name,
        dir_path=project.dir_path,
        def_path=project.def_path,
        status=project.status,
        env_configs=env_configs,
        actions=actions,
        services=services,
        action_handler_configs=action_handler_configs,
    )
    ws_context.ws_projects[project_path] = collected
    return collected


def _collect_services_in_config(
    config: dict[str, Any],
) -> list[domain.ServiceDeclaration]:
    services: list[domain.ServiceDeclaration] = []
    for service_def_raw in config["tool"]["finecode"].get("service", []):
        try:
            service_def = _converter.structure(service_def_raw, config_models.ServiceDefinition)
        except cattrs.ClassValidationError as exception:
            raise config_models.ConfigurationError(str(exception)) from exception

        services.append(
            domain.ServiceDeclaration(
                interface=service_def.interface,
                source=service_def.source,
                env=service_def.env,
                dependencies=service_def.dependencies,
                config=service_def.config,
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
    presets_resolved: bool = True,
) -> list[domain.Action]:
    actions: list[domain.Action] = []
    env_table: dict[str, Any] = config.get("tool", {}).get("finecode", {}).get(
        "env", {}
    )
    for action_name, action_def_raw in (
        config["tool"]["finecode"].get("action", {}).items()
    ):
        try:
            action_def = _converter.structure(action_def_raw, config_models.ActionDefinition)
        except cattrs.ClassValidationError as exception:
            raise config_models.ConfigurationError(str(exception)) from exception

        if action_def.source is None:
            if presets_resolved:
                raise config_models.ConfigurationError(
                    f"Action '{action_name}' has no source. "
                    f"Add 'source = \"<module.ClassName>\"' to "
                    f"[tool.finecode.action.{action_name}] or the preset that declares it."
                )
            # Presets not yet resolved: this entry is a config-only override for an
            # action declared in a preset. Skip silently — the action will be
            # collected once presets are merged into the raw config.
            continue

        new_action = domain.Action(
            name=action_name,
            handlers=[
                domain.ActionHandler(
                    name=handler.name,
                    source=handler.source,
                    config=handler.config or {},
                    env=handler.env,
                    dependencies=handler.dependencies,
                    interpreter=env_table.get(handler.env, {}).get("interpreter"),
                )
                for handler in action_def.handlers
                if handler.enabled
            ],
            source=action_def.source,
            config=action_def.config or {},
        )
        actions.append(new_action)

    return actions
