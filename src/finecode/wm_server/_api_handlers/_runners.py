"""Extension runner management API handlers."""

from __future__ import annotations

import pathlib

from loguru import logger

from finecode.wm_server import context, errors
from finecode.wm_server._api_handlers._helpers import _find_project_by_path
from finecode.wm_server.services import in_flight_runs, next_step
import asyncio


async def _handle_runners_list(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """List all extension runners and their status.

    Result: ``{"runners": [{"projectPath", "envName", "status", "readableId"}]}``
    """
    runners = []
    for (
        project_path,
        runners_by_env,
    ) in ws_context.ws_projects_extension_runners.items():
        for env_name, runner in runners_by_env.items():
            runners.append(
                {
                    "projectPath": str(project_path),
                    "envName": env_name,
                    "status": runner.status.name,
                    "readableId": runner.readable_id,
                }
            )
    return {"runners": runners}


async def _handle_runners_restart(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Restart extension runners, one result per (project, env) target.

    A runner that was restarted but did not come back up is reported in
    ``failed``, not raised — the restart was attempted and its outcome is what
    the caller asked for. Only a target that matches no runner at all raises.

    A project with a run in flight is refused rather than restarted, since
    replacing its runners would kill that run (ADR-0079); ``killInFlightRuns``
    accepts that outcome deliberately.

    Params: ``{"project": "/abs/path", "allProjects": false, "env": "dev_workspace",
    "debug": false, "killInFlightRuns": false}`` — exactly one of ``project`` and
    ``allProjects`` (ADR-0078); ``env`` omitted means every environment of each
    target project.
    Result: ``{"restarted": [{"project", "env", "status"}],
    "failed": [{"project", "env", "status", "error", "nextStep"?}],
    "refused": [{"project", "error", "inFlight"}]}`` — refusal is a property of the
    project, so it is reported once per project rather than once per environment.

    Raises:
        ValueError: the target is unstated, doubly stated, or narrowed by a
            parameter a runner is not addressed by.
        RunnerNotFoundError: the target matches no runner in the workspace.
    """
    from finecode.wm_server.runner import runner_manager

    params = params or {}
    project = params.get("project")
    all_projects = params.get("allProjects", False)
    env = params.get("env")
    debug = params.get("debug", False)
    kill_in_flight_runs = params.get("killInFlightRuns", False)

    if "action" in params:
        raise ValueError(
            "'action' does not narrow a runner restart — a runner is the pair "
            "(project, env) and carries every action of its project. Use "
            "actions/reload to reload a single action."
        )
    if project is not None and all_projects:
        raise ValueError(
            "'project' and 'allProjects' are mutually exclusive — supply the "
            "project path to restart one project, or 'allProjects': true to "
            "restart the whole workspace."
        )
    if project is None and not all_projects:
        raise ValueError(
            "a target is required: pass 'project' with the project path to "
            "restart, or 'allProjects': true to restart the whole workspace."
        )

    runners_by_project = ws_context.ws_projects_extension_runners
    if all_projects:
        project_paths = list(runners_by_project.keys())
    else:
        project_paths = [pathlib.Path(project)]

    targets: list[tuple[pathlib.Path, str]] = []
    for project_path in project_paths:
        env_names = list(runners_by_project.get(project_path, {}))
        if env is not None:
            env_names = [name for name in env_names if name == env]
        targets.extend((project_path, env_name) for env_name in env_names)

    if not targets:
        in_env = f"env '{env}' of " if env is not None else ""
        where = project if project is not None else "any project of the workspace"
        raise errors.RunnerNotFoundError(f"No runner found for {in_env}{where}")

    refused: list[dict] = []
    blocked_projects: set[pathlib.Path] = set()
    for project_path in dict.fromkeys(project_path for project_path, _ in targets):
        blocking = in_flight_runs.blocking_runs(
            ws_context, project_path, kill_in_flight_runs=kill_in_flight_runs
        )
        if blocking:
            blocked_projects.add(project_path)
            refused.append(
                {
                    "project": str(project_path),
                    "error": in_flight_runs.refusal_message(project_path, blocking),
                    "inFlight": in_flight_runs.as_json(blocking),
                }
            )

    # Serial: restarts are already bounded by er_startup_semaphore, and a partial
    # failure must stay attributable to its target.
    restarted: list[dict] = []
    failed: list[dict] = []
    for project_path, env_name in targets:
        if project_path in blocked_projects:
            continue
        try:
            await runner_manager.restart_extension_runner(
                runner_working_dir_path=project_path,
                env_name=env_name,
                ws_context=ws_context,
                debug=debug,
            )
        except runner_manager.RunnerFailedToStart as exception:
            logger.warning(
                f"Runner '{project_path} ({env_name})' did not come back up "
                f"after restart: {exception.message}"
            )
            # The state it stopped in is what distinguishes an environment that
            # needs preparing from a runner that crashed on its own code.
            runner = runners_by_project[project_path][env_name]
            entry = {
                "project": str(project_path),
                "env": env_name,
                "status": runner.status.name,
                "error": exception.message,
            }
            step = next_step.for_runner_failure(
                project_path, env_name, runner.status, exception
            )
            if step is not None:
                entry["nextStep"] = step
            failed.append(entry)
            continue

        runner = runners_by_project[project_path][env_name]
        restarted.append(
            {
                "project": str(project_path),
                "env": env_name,
                "status": runner.status.name,
            }
        )

    return {"restarted": restarted, "failed": failed, "refused": refused}


async def _handle_start_runners(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Start extension runners for all (or specified) projects.

    Complements any runners already running — only missing runners are started.
    Resolves presets so that ``project.actions`` reflects preset-defined handlers.

    Params: ``{"projects": ["project_name", ...]}`` (optional, default: all projects)
    Result: ``{}``
    """

    from finecode.wm_server.runner import runner_manager

    params = params or {}
    project_names: list[str] | None = params.get("projects")
    python_overrides: dict[str, str] | None = params.get("pythonOverrides")
    resolve_presets: bool = params.get("resolvePresets", True)

    # Phase 1: under global lock — decide which projects to start, claim their locks.
    async with ws_context.workspace_state_lock:
        projects = list(ws_context.ws_projects.values())
        if project_names is not None:
            projects = [p for p in projects if str(p.dir_path) in project_names]

        claimed: list = []
        for p in projects:
            init_lock = ws_context.project_init_locks.get(p.dir_path)
            if init_lock is None:
                init_lock = asyncio.Lock()
                ws_context.project_init_locks[p.dir_path] = init_lock
            if not init_lock.locked():
                await init_lock.acquire()  # uncontested → no yield
                claimed.append(p)
            # else: addDir (or another startRunners) already handles this project.
        projects = claimed

    if not projects:
        return {}

    # Phase 2: slow — runner startup outside the global lock.
    from finecode.wm_server.services import runner_start_service

    try:
        await runner_start_service.start_runners_with_auto_prepare(
            projects=projects,
            ws_context=ws_context,
            python_overrides=python_overrides,
            resolve_presets=resolve_presets,
        )
    except runner_manager.RunnerFailedToStart as exc:
        raise ValueError(f"Starting runners failed: {exc.message}") from exc
    finally:
        for p in projects:
            lock = ws_context.project_init_locks.get(p.dir_path)
            if lock is not None and lock.locked():
                lock.release()

    return {}


async def _handle_runners_check_env(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Check whether an environment is valid for a given project.

    Params: ``{"project": "/abs/path/to/project", "envName": "dev_workspace"}``
    Result: ``{"valid": bool}``
    """
    from finecode.wm_server.runner import runner_manager

    params = params or {}
    project_name = params.get("project")
    env_name = params.get("envName")

    if not project_name or not env_name:
        raise ValueError("project and envName are required")

    project = _find_project_by_path(ws_context, project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    check = await runner_manager.check_runner_within_budget(
        ws_context, runner_dir=project.dir_path, env_name=env_name
    )
    return {"valid": check.valid}


async def _handle_runners_remove_env(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Remove an environment for a given project.

    Stops the runner if running, then deletes the environment directory.

    Params: ``{"project": "/abs/path/to/project", "envName": "dev_workspace"}``
    Result: ``{}``
    """
    from finecode.wm_server.runner import runner_manager

    params = params or {}
    project_name = params.get("project")
    env_name = params.get("envName")

    if not project_name or not env_name:
        raise ValueError("project and envName are required")

    project = _find_project_by_path(ws_context, project_name)
    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Stop the runner if it is currently running.
    runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
    runner = runners.get(env_name)
    if runner is not None:
        await runner_manager.stop_extension_runner(runner=runner, ws_context=ws_context)

    await runner_manager.remove_runner_env(
        runner_dir=project.dir_path, env_name=env_name
    )
    return {}
