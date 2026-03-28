"""Shared helpers, parameter parsing, and config-override utilities for API handlers."""
from __future__ import annotations

import asyncio
import pathlib
import typing

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server._jsonrpc import _write_message


# ---------------------------------------------------------------------------
# Server → client (single-client) notification helper
# ---------------------------------------------------------------------------


def _notify_client(writer: asyncio.StreamWriter, method: str, params: dict) -> None:
    """Send a notification to a single client only.

    Unlike ``_notify_all_clients`` (in wm_server.py) this helper targets the
    provided writer, which is useful for streaming partial results back to the
    request originator without broadcasting to every connected client.
    """
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    try:
        _write_message(writer, msg)
    except Exception:
        logger.trace("FineCode API: failed to notify client, skipping")


# ---------------------------------------------------------------------------
# Project lookup helpers
# ---------------------------------------------------------------------------


def _project_to_dict(project: domain.Project) -> dict:
    return {
        "name": project.name,
        "path": str(project.dir_path),
        "status": project.status.name,
    }


def _find_project_by_path(
    ws_context: context.WorkspaceContext, project_path: str
) -> domain.Project | None:
    """Look up a project by its absolute directory path (canonical external identifier)."""
    return ws_context.ws_projects.get(pathlib.Path(project_path))


# ---------------------------------------------------------------------------
# Parameter parsing helpers (shared by run / runBatch and their +progress
# variants to avoid duplication)
# ---------------------------------------------------------------------------


class _RunActionParams(typing.NamedTuple):
    action_name: str
    project: domain.CollectedProject
    action_params: dict
    options: dict
    result_formats: list  # list[run_service.RunResultFormat]
    trigger: typing.Any  # run_service.RunActionTrigger
    dev_env: typing.Any  # run_service.DevEnv


class _RunBatchParams(typing.NamedTuple):
    actions: list[str]
    project_names: list[str] | None
    action_params: dict
    params_by_project: dict[str, dict]
    concurrently: bool
    result_format_strs: list[str]
    result_formats: list  # list[run_service.RunResultFormat]
    trigger: typing.Any  # run_service.RunActionTrigger
    dev_env: typing.Any  # run_service.DevEnv


def _parse_and_validate_run_action_params(
    params: dict, ws_context: context.WorkspaceContext
) -> _RunActionParams:
    """Extract, validate, and parse ``actions/run`` request parameters."""
    # Import run_service here to avoid circular imports
    from finecode.wm_server.services import run_service

    action_name = params.get("action")
    project_name = params.get("project")
    action_params = params.get("params", {})
    options = params.get("options", {})

    if not action_name:
        raise ValueError("action parameter is required")
    if not project_name:
        raise ValueError("project parameter is required")

    # Find the project by its absolute directory path (canonical external identifier)
    project = _find_project_by_path(ws_context, project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")
    if not isinstance(project, domain.CollectedProject):
        raise ValueError(
            f"Project '{project_name}' actions are not collected yet. "
            "Ensure the project is initialized before running actions."
        )

    result_format_strs: list[str] = options.get("resultFormats", ["json"])
    result_formats = [
        run_service.RunResultFormat(fmt)
        for fmt in result_format_strs
        if fmt in ("json", "string")
    ]
    trigger = run_service.RunActionTrigger(options.get("trigger", "unknown"))
    dev_env = run_service.DevEnv(options.get("devEnv", "cli"))

    return _RunActionParams(
        action_name=action_name,
        project=project,
        action_params=action_params,
        options=options,
        result_formats=result_formats,
        trigger=trigger,
        dev_env=dev_env,
    )


def _parse_run_batch_params(params: dict) -> _RunBatchParams:
    """Extract and parse ``actions/runBatch`` request parameters."""
    # Import run_service here to avoid circular imports
    from finecode.wm_server.services import run_service

    actions: list[str] = params.get("actions", [])
    project_names: list[str] | None = params.get("projects")
    action_params: dict = params.get("params", {})
    params_by_project: dict[str, dict] = params.get("paramsByProject", {})
    options: dict = params.get("options", {})

    concurrently: bool = options.get("concurrently", False)
    result_format_strs: list[str] = options.get("resultFormats", ["string"])
    result_formats = [
        run_service.RunResultFormat(fmt)
        for fmt in result_format_strs
        if fmt in ("json", "string")
    ]
    trigger = run_service.RunActionTrigger(options.get("trigger", "user"))
    dev_env = run_service.DevEnv(options.get("devEnv", "cli"))

    return _RunBatchParams(
        actions=actions,
        project_names=project_names,
        action_params=action_params,
        params_by_project=params_by_project,
        concurrently=concurrently,
        result_format_strs=result_format_strs,
        result_formats=result_formats,
        trigger=trigger,
        dev_env=dev_env,
    )


def _resolve_actions_by_project(
    project_names: list[str] | None,
    actions: list[str],
    ws_context: context.WorkspaceContext,
) -> dict[pathlib.Path, list[str]]:
    """Resolve the ``actions_by_project`` mapping for ``runBatch`` requests.

    When *project_names* is given, builds the map from those paths; otherwise
    auto-discovers which projects expose the requested actions.
    Raises ``ValueError`` if a named project is not found, or if no projects
    expose the requested actions in the auto-discover branch.
    """
    from finecode.wm_server.services import run_service

    if project_names is not None:
        actions_by_project: dict[pathlib.Path, list[str]] = {}
        for project_path_str in project_names:
            project = _find_project_by_path(ws_context, project_path_str)
            if project is None:
                raise ValueError(f"Project '{project_path_str}' not found")
            actions_by_project[project.dir_path] = list(actions)
    else:
        actions_by_project = run_service.find_projects_with_actions(ws_context, actions)
        if not actions_by_project:
            all_projects = list(ws_context.ws_projects.keys())
            projects_with_actions = {
                str(p): [a.name for a in proj.actions]
                for p, proj in ws_context.ws_projects.items()
                if hasattr(proj, "actions") and proj.actions
            }
            logger.warning(
                f"runBatch: no projects found with actions={actions}. "
                f"Known projects: {[str(p) for p in all_projects]}. "
                f"Actions per project: {projects_with_actions}"
            )
            raise ValueError(f"No projects found with actions: {actions}")

    return actions_by_project


def _build_batch_result(
    result_by_project: dict[pathlib.Path, dict[str, typing.Any]],
) -> tuple[dict[str, dict], int]:
    """Aggregate per-project action results into the ``runBatch`` response shape."""
    overall_return_code = 0
    results: dict[str, dict] = {}
    for project_path, actions_result in result_by_project.items():
        project_results: dict[str, dict] = {}
        for action_name, response in actions_result.items():
            overall_return_code |= response.return_code
            project_results[action_name] = {
                "resultByFormat": response.result_by_format,
                "returnCode": response.return_code,
            }
        results[str(project_path)] = project_results
    return results, overall_return_code


def _apply_config_overrides_to_projects(
    projects: list[domain.Project],
    actions: list[str],
    config_overrides: dict[str, dict[str, dict[str, typing.Any]]],
) -> dict[pathlib.Path, dict[str, dict[str, typing.Any]]]:
    """Apply handler config overrides to project.action_handler_configs.

    ``config_overrides`` format: ``{action_name: {handler_name_or_"": {param: value}}}``
    where the empty-string key ``""`` means all handlers of that action.

    Returns the original ``action_handler_configs`` per project.
    """
    originals: dict[pathlib.Path, dict[str, dict[str, typing.Any]]] = {}
    actions_set = set(actions)
    for project in projects:
        if not isinstance(project, domain.CollectedProject):
            continue
        originals[project.dir_path] = {
            source: dict(cfg)
            for source, cfg in project.action_handler_configs.items()
        }
        for action in project.actions:
            if action.name not in actions_set:
                continue
            action_overrides = config_overrides.get(action.name, {})
            if not action_overrides:
                continue
            action_level = action_overrides.get("", {})
            for handler in action.handlers:
                handler_specific = action_overrides.get(handler.name, {})
                merged = {**action_level, **handler_specific}
                if merged:
                    project.action_handler_configs[handler.source] = {
                        **(project.action_handler_configs.get(handler.source) or {}),
                        **merged,
                    }
    return originals
