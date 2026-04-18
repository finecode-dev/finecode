"""Extension runner management API handlers."""
from __future__ import annotations

import pathlib

from finecode.wm_server import context
from finecode.wm_server._api_handlers._helpers import _find_project_by_path


async def _handle_runners_list(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """List all extension runners and their status.

    Result: ``{"runners": [{"projectPath", "envName", "status", "readableId"}]}``
    """
    runners = []
    for project_path, runners_by_env in ws_context.ws_projects_extension_runners.items():
        for env_name, runner in runners_by_env.items():
            runners.append({
                "projectPath": str(project_path),
                "envName": env_name,
                "status": runner.status.name,
                "readableId": runner.readable_id,
            })
    return {"runners": runners}


async def _handle_runners_restart(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Restart a specific extension runner.

    Params: ``{"runnerWorkingDir": "/abs/path", "envName": "dev_workspace", "debug": false}``
    Result: ``{}``
    """
    from finecode.wm_server.runner import runner_manager

    params = params or {}
    runner_working_dir = params.get("runnerWorkingDir")
    env_name = params.get("envName")
    debug = params.get("debug", False)

    if not runner_working_dir or not env_name:
        raise ValueError("runner_working_dir and env_name are required")

    await runner_manager.restart_extension_runner(
        runner_working_dir_path=pathlib.Path(runner_working_dir),
        env_name=env_name,
        ws_context=ws_context,
        debug=debug,
    )
    return {}


async def _handle_start_runners(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Start extension runners for all (or specified) projects.

    Complements any runners already running — only missing runners are started.
    Resolves presets so that ``project.actions`` reflects preset-defined handlers.

    Params: ``{"projects": ["project_name", ...]}`` (optional, default: all projects)
    Result: ``{}``
    """
    import asyncio

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
    try:
        await runner_manager.start_runners_with_presets(
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

    valid = await runner_manager.check_runner(
        runner_dir=project.dir_path, env_name=env_name
    )
    return {"valid": valid}


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
        await runner_manager.stop_extension_runner(runner=runner)

    runner_manager.remove_runner_venv(runner_dir=project.dir_path, env_name=env_name)
    return {}
