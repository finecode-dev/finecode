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
# Action lookup by source (ADR-0019: import-path aliases as action identifiers)
# ---------------------------------------------------------------------------


async def find_action_by_source(
    actions: list[domain.Action],
    source: str,
    project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> domain.Action | None:
    """Find an action by an import-path alias (ADR-0019).

    Resolution is a two-step process:

    1. Direct match against the action's config ``source`` field and its
       ``canonical_source`` (set by the WM after ``finecodeRunner/updateConfig``
       via ``finecodeRunner/resolveActionSources``).  This covers the vast majority of
       calls where callers use the same alias written in project configuration or
       the canonical path returned by ``actions/list``.

    2. If no match is found, ask a running ER to import the alias and return its
       canonical path (``__module__ + "." + __qualname__``), then retry the match
       against ``canonical_source``.  This covers arbitrary re-export aliases that
       resolve to the same class (full ADR-0019 support).  Envs that declare
       handlers for any of the project's known actions are tried first (they are
       guaranteed to have the relevant extension packages installed);
       ``dev_workspace`` and any other running runner are tried as a fallback.
    """
    # Step 1: direct match.
    action = next(
        (
            a
            for a in actions
            if a.source == source
            or (a.canonical_source is not None and a.canonical_source == source)
        ),
        None,
    )
    if action is not None:
        return action

    # Step 2: ask an ER to resolve the alias.
    from finecode.wm_server.runner import runner_client as rc

    runners_by_env = ws_context.ws_projects_extension_runners.get(project.dir_path, {})

    # Prefer envs where handlers of known actions are declared — those envs are
    # guaranteed to have the relevant extension packages installed. Since we don't
    # yet know *which* action we're resolving, we collect handler envs across all
    # known actions. Fall back to dev_workspace, then any other running runner.
    seen_envs: set[str] = set()
    handler_envs: list[str] = []
    for a in actions:
        for h in a.handlers:
            if h.env not in seen_envs:
                seen_envs.add(h.env)
                handler_envs.append(h.env)
    env_order = handler_envs + [
        e for e in (["dev_workspace"] + [e for e in runners_by_env if e != "dev_workspace"])
        if e not in seen_envs
    ]
    for env_name in env_order:
        runner = runners_by_env.get(env_name)
        if runner is None or runner.status != rc.RunnerStatus.RUNNING:
            continue
        try:
            canonical = await rc.resolve_source(runner, source)
        except Exception as exc:
            logger.debug(f"find_action_by_source: ER '{env_name}' failed for '{source}': {exc}")
            continue
        if canonical is None:
            continue
        action = next(
            (a for a in actions if a.canonical_source == canonical),
            None,
        )
        if action is not None:
            return action

    return None


# ---------------------------------------------------------------------------
# Parameter parsing helpers (shared by run / runBatch and their +progress
# variants to avoid duplication)
# ---------------------------------------------------------------------------


class _RunActionParams(typing.NamedTuple):
    action: domain.Action
    project: domain.CollectedProject
    action_params: dict
    options: dict
    result_formats: list  # list[run_service.RunResultFormat]
    trigger: typing.Any  # run_service.RunActionTrigger
    dev_env: typing.Any  # run_service.DevEnv


class _RunBatchParams(typing.NamedTuple):
    action_sources: list[str]
    project_names: list[str] | None
    action_params: dict
    params_by_project: dict[str, dict]
    concurrently: bool
    result_format_strs: list[str]
    result_formats: list  # list[run_service.RunResultFormat]
    trigger: typing.Any  # run_service.RunActionTrigger
    dev_env: typing.Any  # run_service.DevEnv


async def _parse_and_validate_run_action_params(
    params: dict, ws_context: context.WorkspaceContext
) -> _RunActionParams:
    """Extract, validate, and parse ``actions/run`` request parameters."""
    # Import run_service here to avoid circular imports
    from finecode.wm_server.services import run_service

    action_source = params.get("actionSource")
    project_name = params.get("project")
    action_params = params.get("params", {})
    options = params.get("options", {})

    if not action_source:
        raise ValueError("actionSource parameter is required")
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

    action = await find_action_by_source(project.actions, action_source, project, ws_context)
    if action is None:
        raise ValueError(
            f"Action with source '{action_source}' not found in project '{project_name}'"
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
        action=action,
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

    action_sources: list[str] = params.get("actionSources", [])
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
        action_sources=action_sources,
        project_names=project_names,
        action_params=action_params,
        params_by_project=params_by_project,
        concurrently=concurrently,
        result_format_strs=result_format_strs,
        result_formats=result_formats,
        trigger=trigger,
        dev_env=dev_env,
    )


async def _resolve_actions_by_project(
    project_names: list[str] | None,
    action_sources: list[str],
    ws_context: context.WorkspaceContext,
) -> tuple[dict[pathlib.Path, list[str]], dict[str, str]]:
    """Resolve the ``actions_by_project`` mapping for ``runBatch`` requests.

    Accepts *action_sources* (import-path aliases per ADR-0019) and returns:
    - ``actions_by_project``: ``{project_path: [action_name, ...]}``, used
      internally by the execution pipeline (which is still name-based).
    - ``name_to_source``: ``{action_name: requested_source}``, used to re-key
      the batch result by the caller-provided source rather than the internal
      action name.

    When *project_names* is given, builds the map from those paths; otherwise
    auto-discovers which projects expose the requested actions.
    Raises ``ValueError`` if a named project is not found, or if no projects
    expose the requested actions in the auto-discover branch.
    """
    from finecode.wm_server.services import run_service

    name_to_source: dict[str, str] = {}

    if project_names is not None:
        actions_by_project: dict[pathlib.Path, list[str]] = {}
        for project_path_str in project_names:
            project = _find_project_by_path(ws_context, project_path_str)
            if project is None:
                raise ValueError(f"Project '{project_path_str}' not found")
            if not isinstance(project, domain.CollectedProject):
                actions_by_project[project.dir_path] = []
                continue

            project_action_names: list[str] = []
            for source in action_sources:
                action = await find_action_by_source(
                    project.actions, source, project, ws_context
                )
                if action is not None:
                    project_action_names.append(action.name)
                    name_to_source[action.name] = source
            actions_by_project[project.dir_path] = project_action_names
    else:
        # Auto-discover: find projects that have at least one of the requested actions.
        actions_by_project = {}
        for project in ws_context.ws_projects.values():
            if not isinstance(project, domain.CollectedProject):
                continue
            project_action_names = []
            for source in action_sources:
                action = await find_action_by_source(
                    project.actions, source, project, ws_context
                )
                if action is not None:
                    project_action_names.append(action.name)
                    name_to_source[action.name] = source
            if project_action_names:
                actions_by_project[project.dir_path] = project_action_names

        if not actions_by_project:
            all_projects = list(ws_context.ws_projects.keys())
            projects_with_actions = {
                str(p): [a.name for a in proj.actions]
                for p, proj in ws_context.ws_projects.items()
                if hasattr(proj, "actions") and proj.actions
            }
            logger.warning(
                f"runBatch: no projects found with actionSources={action_sources}. "
                f"Known projects: {[str(p) for p in all_projects]}. "
                f"Actions per project: {projects_with_actions}"
            )
            raise ValueError(f"No projects found with actionSources: {action_sources}")

    return actions_by_project, name_to_source


def _build_batch_result(
    result_by_project: dict[pathlib.Path, dict[str, typing.Any]],
    name_to_source: dict[str, str],
) -> tuple[dict[str, dict], int]:
    """Aggregate per-project action results into the ``runBatch`` response shape.

    Result keys use the caller-provided source strings (from the request's
    ``actionSources``) rather than internal action names.
    """
    overall_return_code = 0
    results: dict[str, dict] = {}
    for project_path, actions_result in result_by_project.items():
        project_results: dict[str, dict] = {}
        for action_name, response in actions_result.items():
            overall_return_code |= response.return_code
            key = name_to_source.get(action_name, action_name)
            project_results[key] = {
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
