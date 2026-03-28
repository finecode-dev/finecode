"""Action run and management API handlers."""
from __future__ import annotations

import asyncio
import pathlib

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._helpers import (
    _apply_config_overrides_to_projects,
    _build_batch_result,
    _find_project_by_path,
    _parse_and_validate_run_action_params,
    _parse_run_batch_params,
    _resolve_actions_by_project,
)
from finecode.wm_server.services.action_tree import _handle_get_tree  # noqa: F401 (re-export)


async def _handle_run_action(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Run an action on a project."""
    from finecode.wm_server.services import run_service

    parsed = _parse_and_validate_run_action_params(params or {}, ws_context)

    try:
        result = await run_service.run_action(
            action_name=parsed.action_name,
            params=parsed.action_params,
            project_def=parsed.project,
            ws_context=ws_context,
            run_trigger=parsed.trigger,
            dev_env=parsed.dev_env,
            result_formats=parsed.result_formats,
            initialize_all_handlers=True,
        )
        return {
            "resultByFormat": result.result_by_format,
            "returnCode": result.return_code,
        }
    except run_service.ActionRunFailed:
        raise


async def _handle_actions_reload(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Reload an action's handlers in all relevant extension runners.

    Params: ``{"actionNodeId": "project_path::action_name"}``
    Result: ``{}``
    """
    from finecode.wm_server.runner import runner_client

    params = params or {}
    action_node_id = params.get("actionNodeId", "")
    parts = action_node_id.split("::")
    if len(parts) < 2:
        raise ValueError(f"Invalid action_node_id: {action_node_id!r}")

    project_path = pathlib.Path(parts[0])
    action_name = parts[1]

    runners_by_env = ws_context.ws_projects_extension_runners.get(project_path, {})
    for runner in runners_by_env.values():
        await runner_client.reload_action(runner, action_name)

    return {}


async def _handle_run_batch(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Run multiple actions across multiple (or all) projects.

    Params:
      actions: list[str] - action names to run
      projects: list[str] | None - project paths (absolute) to filter; absent/null means all projects
      params: dict - action payload shared across all projects
      params_by_project: dict[str, dict] - per-project payload overrides keyed by project path string
      options:
        concurrently: bool - run actions concurrently within each project (default false)
        result_formats: list[str] - "string" and/or "json" (default ["string"])
        trigger: str - run trigger (default "user")
        dev_env: str - dev environment (default "cli")

    Result: {"results": {project_path_str: {action_name: {"resultByFormat": ..., "returnCode": int}}},
       "returnCode": int}
    """
    from finecode.wm_server.services import run_service

    parsed = _parse_run_batch_params(params or {})

    if not parsed.actions:
        raise ValueError("actions list is required and must be non-empty")

    logger.debug(f"runBatch: actions={parsed.actions} projects={parsed.project_names} formats={parsed.result_format_strs}")

    actions_by_project = _resolve_actions_by_project(parsed.project_names, parsed.actions, ws_context)

    await run_service.start_required_environments(
        actions_by_project, ws_context, update_config_in_running_runners=True
    )

    result_by_project = await run_service.run_actions_in_projects(
        actions_by_project=actions_by_project,
        action_payload=parsed.action_params,
        ws_context=ws_context,
        concurrently=parsed.concurrently,
        result_formats=parsed.result_formats,
        run_trigger=parsed.trigger,
        dev_env=parsed.dev_env,
        payload_overrides_by_project=parsed.params_by_project,
    )

    results, overall_return_code = _build_batch_result(result_by_project)
    logger.debug(f"runBatch: done, projects_count={len(results)} returnCode={overall_return_code}")
    return {"results": results, "returnCode": overall_return_code}


async def _handle_server_reset(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Reset the server state.

    Result: ``{}``
    """
    logger.info("FineCode API: server reset requested")
    return {}


async def _handle_set_config_overrides(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Handle ``workspace/setConfigOverrides``.

    Stores handler config overrides persistently in the workspace context so that
    they are applied to all subsequent action runs. These overrides survive across
    multiple requests and do not require runners to be stopped first.

    If extension runners are already running they receive a config-update push
    immediately; their initialized handlers are dropped and will be re-initialized
    with the new config on the next run.
    """
    from finecode.wm_server.runner import runner_manager
    from finecode.wm_server.runner.runner_client import RunnerStatus

    params = params or {}
    overrides: dict = params.get("overrides", {})

    ws_context.handler_config_overrides = overrides

    # Apply to all existing project domain objects so that project.action_handler_configs
    # reflects the new overrides
    all_projects = list(ws_context.ws_projects.values())
    action_names = list(overrides.keys())
    if all_projects and action_names:
        _apply_config_overrides_to_projects(all_projects, action_names, overrides)

    # Push the updated config to any already-running runners so they drop their
    # initialized handlers and pick up the new config on the next invocation.
    try:
        async with asyncio.TaskGroup() as tg:
            for project_path, runners_by_env in ws_context.ws_projects_extension_runners.items():
                project = ws_context.ws_projects.get(project_path)
                if project is None or not isinstance(project, domain.CollectedProject):
                    continue
                for runner in runners_by_env.values():
                    if runner.status == RunnerStatus.RUNNING:
                        tg.create_task(
                            runner_manager.update_runner_config(
                                runner=runner,
                                project=project,
                                handlers_to_initialize=None,
                                ws_context=ws_context,
                            )
                        )
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.warning(f"Failed to push config update to runner: {exc}")

    return {}


async def _handle_get_payload_schemas(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Return payload schemas for the given actions in a project.

    Params: ``{"project": "/abs/path/to/project", "action_names": ["lint", "format"]}``
    Result: ``{"schemas": {"lint": {...} | null, "format": {...} | null}}``

    Schemas are fetched on-demand from Extension Runners. The ``dev_workspace``
    runner is tried first (fast path). For actions whose class is not importable
    there, the runner for each handler env is tried as a fallback.

    Results are cached in ``ws_context.ws_action_schemas``.
    """
    from finecode.wm_server.runner import runner_client

    params = params or {}
    project_path = params.get("project")
    action_names: list[str] = params.get("action_names", [])

    if not project_path:
        raise ValueError("project parameter is required")

    project = _find_project_by_path(ws_context, project_path)
    if project is None:
        raise ValueError(f"Project '{project_path}' not found")
    if not isinstance(project, domain.CollectedProject):
        raise ValueError(
            f"Project '{project_path}' actions are not collected yet. "
            "Ensure the project is initialized before requesting schemas."
        )

    cache = ws_context.ws_action_schemas.setdefault(project.dir_path, {})
    missing = [name for name in action_names if name not in cache]

    if missing:
        runners_by_env = ws_context.ws_projects_extension_runners.get(project.dir_path, {})

        # Phase 1: query dev_workspace runner (covers all finecode_extension_api actions)
        dev_runner = runners_by_env.get("dev_workspace")
        if dev_runner is not None and dev_runner.status == runner_client.RunnerStatus.RUNNING:
            try:
                schemas = await runner_client.get_payload_schemas(dev_runner)
                cache.update(schemas)
            except Exception as exc:
                logger.debug(f"Failed to get payload schemas from dev_workspace runner: {exc}")

        # Phase 2: for actions still None, try the handler env runners
        still_missing = [name for name in missing if cache.get(name) is None]
        for action_name in still_missing:
            action = next((a for a in project.actions if a.name == action_name), None)
            if action is None:
                continue
            envs_to_try = {h.env for h in action.handlers if h.env and h.env != "dev_workspace"}
            for env_name in envs_to_try:
                runner = runners_by_env.get(env_name)
                if runner is None or runner.status != runner_client.RunnerStatus.RUNNING:
                    continue
                try:
                    schemas = await runner_client.get_payload_schemas(runner)
                    if schemas.get(action_name) is not None:
                        cache[action_name] = schemas[action_name]
                        break
                except Exception as exc:
                    logger.debug(
                        f"Failed to get payload schemas from runner '{env_name}': {exc}"
                    )

    return {"schemas": {name: cache.get(name) for name in action_names}}
