"""Workspace and project API handlers."""
from __future__ import annotations

import asyncio
import pathlib
from typing import cast

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server._api_handlers._helpers import (
    _apply_config_overrides_to_projects,
    _find_project_by_path,
    _project_to_dict,
)


async def _handle_list_projects(
    params: dict | None, ws_context: context.WorkspaceContext
) -> list[dict]:
    """List all projects. Params: {}. Result: [{name, path, status}]."""
    return [_project_to_dict(p) for p in ws_context.ws_projects.values()]


async def _handle_get_workspace_editable_packages(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Return workspace editable packages as name → absolute posix path.

    Result: ``{"packages": {"pkg_name": "/abs/path", ...}}``
    """
    return {
        "packages": {
            name: path.as_posix()
            for name, path in ws_context.ws_editable_packages.items()
        }
    }


async def _handle_get_project_raw_config(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Return the resolved raw config for a project by path.

    Params: ``{"project": "/abs/path/to/project"}``
    Result: ``{"rawConfig": {...}}``
    """
    params = params or {}
    project_path = params.get("project")
    if not project_path:
        raise ValueError("project parameter is required")

    project = _find_project_by_path(ws_context, project_path)
    if project is None:
        raise ValueError(f"Project '{project_path}' not found")

    raw_config = ws_context.ws_projects_raw_configs.get(project.dir_path, {})
    return {"rawConfig": raw_config}


async def _handle_find_project_for_file(
    params: dict, ws_context: context.WorkspaceContext
) -> dict:
    """Return project directory path containing a given file.

    It finds the *nearest* project in the
    workspace that actually "uses finecode" (i.e. has a valid config).  The
    project is determined purely based on path containment.

    **Params:** ``{"filePath": "/abs/path/to/file"}``
    **Result:** ``{"project": "/abs/path/to/project"}`` or ``{"project": null}`` if
    the file does not belong to any suitable project.
    """

    file_path = pathlib.Path(params["filePath"])

    # iterate over known projects in reverse-sorted order so that nested/child
    # projects are considered before their parents.  This mirrors the behaviour
    # in ``find_project_with_action_for_file`` but without any action-specific
    # checks.
    sorted_dirs = list(ws_context.ws_projects.keys())
    # reverse sort by path (string) ensures children come first
    sorted_dirs.sort(reverse=True)

    for project_dir in sorted_dirs:
        if file_path.is_relative_to(project_dir):
            project = ws_context.ws_projects[project_dir]
            if project.status == domain.ProjectStatus.CONFIG_VALID:
                return {"project": str(project.dir_path)}
            # skip projects that aren't using finecode
            continue

    # not in any project or none of the containing projects are CONFIG_VALID
    return {"project": None}


async def _handle_add_dir(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Add a workspace directory. Discovers projects, reads configs, starts runners.

    Params:
      dir_path: str - absolute path to the workspace directory
      start_runners: bool - whether to start extension runners (default true).
        When false, configs are read and actions collected without starting any
        runners. Useful when runner environments may not exist yet (e.g. before
        running prepare-envs).
      projects: list[str] | null - optional list of project paths (absolute) to initialize.
        Projects not in this list are discovered but not config-initialized or
        started. Omit (or pass null) to initialize all projects.
        Calling add_dir again for the same dir with a different filter (or no
        filter) will initialize the previously skipped projects.
    """
    from finecode.wm_server.config import collect_actions, read_configs
    from finecode.wm_server.runner import runner_manager
    from finecode.wm_server.runner.runner_client import RunnerStatus

    logger.trace(f"Add ws dir: {params}")

    # ── Phase 1: fast, under global lock ────────────────────────────────────────
    # Dir tracking, filesystem scan, and deciding which projects need work.
    # read_projects_in_dir has no awaits inside, so the global lock is never
    # yielded during this phase.
    async with ws_context.workspace_state_lock:
        params = params or {}
        dir_path = pathlib.Path(params["dirPath"])
        start_runners: bool = params.get("startRunners", True)
        projects_filter: set[str] | None = (
            set(params["projects"]) if params.get("projects") else None
        )
        logger.trace(f"Add ws dir: {dir_path}")

        is_new_dir = dir_path not in ws_context.ws_dirs_paths
        if is_new_dir:
            ws_context.ws_dirs_paths.append(dir_path)
            await read_configs.read_projects_in_dir(dir_path, ws_context)
            ws_context.ws_editable_packages = read_configs.resolve_workspace_editable_packages(ws_context)

        # Projects in this dir that haven't been config-initialized yet, covering
        # both newly discovered projects and ones filtered out by a previous call.
        projects_to_init = [
            p for p in ws_context.ws_projects.values()
            if p.dir_path.is_relative_to(dir_path)
            and p.dir_path not in ws_context.ws_projects_raw_configs
        ]

        if projects_filter is not None:
            projects_to_init = [p for p in projects_to_init if str(p.dir_path) in projects_filter]

        # Claim per-project initialization locks before releasing the global lock.
        # acquire() on a freshly created, uncontested Lock completes without yielding,
        # so we don't give up the global lock here.  Any concurrent addDir that enters
        # Phase 1 after us will find these locks already taken and skip those projects.
        claimed_projects: list[domain.Project] = []
        wait_for_locks: list[asyncio.Lock] = []
        for project in projects_to_init:
            init_lock = ws_context.project_init_locks.get(project.dir_path)
            if init_lock is None:
                init_lock = asyncio.Lock()
                ws_context.project_init_locks[project.dir_path] = init_lock
            if not init_lock.locked():
                await init_lock.acquire()  # uncontested → no yield
                claimed_projects.append(project)
            else:
                # Another concurrent addDir is initializing this project.
                # Record the lock so we can wait for it outside the global lock.
                wait_for_locks.append(init_lock)
        projects_to_init = claimed_projects

        # Also wait for projects that already have raw configs but whose init lock
        # is still held — this means another addDir has read their config but
        # hasn't finished starting runners yet (Phase 2 in progress).  Without
        # this check, we'd return before those projects become CollectedProjects.
        already_configured = [
            p for p in ws_context.ws_projects.values()
            if p.dir_path.is_relative_to(dir_path)
            and p.dir_path in ws_context.ws_projects_raw_configs
        ]
        if projects_filter is not None:
            already_configured = [p for p in already_configured if str(p.dir_path) in projects_filter]
        for project in already_configured:
            init_lock = ws_context.project_init_locks.get(project.dir_path)
            if init_lock is not None and init_lock.locked():
                wait_for_locks.append(init_lock)

        if not projects_to_init and not wait_for_locks:
            return {"projects": []}

    # ── Phase 2: slow, global lock released ──────────────────────────────────
    # Config reading and runner startup happen here, protected only by the
    # per-project locks claimed above.  Unrelated projects and other operations
    # (removeDir, startRunners for different projects) can now proceed concurrently.

    if not projects_to_init:
        # We claimed nothing, but other concurrent addDir calls are initializing
        # our projects.  Wait for them to finish before returning so that callers
        # (e.g. the MCP server's list_tools) see fully-initialized CollectedProject
        # instances immediately after add_dir returns.
        for lock in wait_for_locks:
            async with lock:
                pass  # wait until the lock is released by the owner
        return {"projects": []}

    try:
        for project in projects_to_init:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=False
            )

        if not start_runners:
            # Collect actions directly from raw config without needing runners.
            from finecode.wm_server.config import config_models
            for project in projects_to_init:
                if project.status == domain.ProjectStatus.CONFIG_VALID:
                    try:
                        collect_actions.collect_project(
                            project_path=project.dir_path, ws_context=ws_context
                        )
                    except config_models.ConfigurationError as exc:
                        logger.warning(
                            f"Failed to collect actions for {project.name}: {exc.message}"
                        )
            return {"projects": [_project_to_dict(p) for p in projects_to_init]}

        try:
            await runner_manager.start_runners_with_presets(
                projects=projects_to_init,
                ws_context=ws_context,
                initialize_all_handlers=True,
            )
        except runner_manager.RunnerFailedToStart as exc:
            from finecode.wm_server import wm_server as _wm
            _wm._notify_all_clients("server/userMessage", {
                "message": f"Starting runners failed: {exc.message}. "
                           f"Did you run `finecode prepare-envs`?",
                "type": "ERROR",
            })
            raise

        # If config overrides were set before this addDir call (e.g. standalone CLI mode),
        # apply them to the newly discovered projects and push to their running runners.
        if ws_context.handler_config_overrides and projects_to_init:
            # Re-fetch projects from ws_context: start_runners_with_presets upgrades
            # plain Project instances to CollectedProject/ResolvedProject in-place there.
            collected_projects = [
                p for p in (ws_context.ws_projects.get(p.dir_path) for p in projects_to_init)
                if isinstance(p, domain.CollectedProject)
            ]
            action_names = list(ws_context.handler_config_overrides.keys())
            _apply_config_overrides_to_projects(
                cast(list[domain.Project], collected_projects),
                action_names,
                ws_context.handler_config_overrides,
            )
            try:
                async with asyncio.TaskGroup() as tg:
                    for project in collected_projects:
                        runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
                        for runner in runners.values():
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

        return {"projects": [_project_to_dict(p) for p in projects_to_init]}
    finally:
        for project in projects_to_init:
            lock = ws_context.project_init_locks.get(project.dir_path)
            if lock is not None and lock.locked():
                lock.release()


async def _handle_remove_dir(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Remove a workspace directory. Stops runners, removes affected projects."""
    from finecode.wm_server.runner import runner_manager

    params = params or {}
    dir_path = pathlib.Path(params["dirPath"])
    logger.trace(f'Remove ws dir: {dir_path}')

    async with ws_context.workspace_state_lock:
        ws_context.ws_dirs_paths.remove(dir_path)

        for project_dir in list(ws_context.ws_projects.keys()):
            if not project_dir.is_relative_to(dir_path):
                continue

            # Keep if the project is also under another remaining ws_dir.
            keep = any(
                project_dir.is_relative_to(d) for d in ws_context.ws_dirs_paths
            )
            if keep:
                continue

            runners = ws_context.ws_projects_extension_runners.get(project_dir, {})
            for runner in runners.values():
                await runner_manager.stop_extension_runner(runner=runner)
            del ws_context.ws_projects[project_dir]
            ws_context.ws_projects_raw_configs.pop(project_dir, None)

    return {}


async def _handle_list_actions(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """List available actions, optionally filtered by project path."""
    project_filter = (params or {}).get("project")
    actions = []
    for project in ws_context.ws_projects.values():
        if project_filter and str(project.dir_path) != project_filter:
            continue
        if not isinstance(project, domain.CollectedProject):
            continue
        for action in project.actions:
            actions.append({
                "name": action.name,
                "source": action.source,
                "project": str(project.dir_path),
                "handlers": [
                    {"name": h.name, "source": h.source, "env": h.env}
                    for h in action.handlers
                ],
            })
    return {"actions": actions}
