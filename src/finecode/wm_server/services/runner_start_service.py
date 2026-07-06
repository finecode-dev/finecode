"""High-level runner startup service — the single public entry point for starting runners.

Public API
----------
- ``start_runners_with_auto_prepare``: start runners for a list of projects; automatically
  prepares environments on failure (runs ``create_envs`` / ``install_envs`` as needed).
- ``get_or_start_runners_with_presets``: lazily start the dev_workspace runner for a single
  project (with auto-prepare); returns immediately if already running/initializing.

Both functions wrap ``runner_manager.start_runners_with_presets``, which is an internal
implementation detail of this module — external callers should not import it directly.

Environment auto-repair behaviour
----------------------------------
- ``NO_VENV`` projects: runs ``create_envs`` then ``install_envs``.
- ``FAILED`` projects: runs ``install_envs`` only (execution environment exists but a
  required package is missing).
- No-runner projects (task was cancelled before the runner object was created): treated
  like ``FAILED`` — ``install_env_for_project`` is idempotent and handles both missing
  and existing venvs.
- ``RUNNING``-but-unresolved projects (runner started successfully but
  ``_start_runner_and_read_config`` was cancelled before preset resolution completed):
  not restarted (env is healthy), but included in the retry so that
  ``_start_runner_and_read_config`` can finish and upgrade them to ``ResolvedProject``.

Cascade prevention
------------------
``runner_manager.start_runners_with_presets`` uses ``asyncio.gather`` (not
``asyncio.TaskGroup``) so that one project failing does not cancel sibling tasks.
All projects run to completion and ``_auto_prepare_and_retry`` sees the full set of
broken environments in a single pass.
"""
from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from loguru import logger

import finecode_jsonrpc as _jsonrpc_client
from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_manager

if TYPE_CHECKING:
    from finecode.wm_server.runner import runner_client

# Re-export for callers that only import this module.
RunnerFailedToStart = _jsonrpc_client.ServerFailedToStart
RunnerConfigurationError = runner_manager.ServerConfigurationError


async def _auto_prepare_and_retry(
    exc: Exception,
    projects: list[domain.Project],
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool,
) -> None:
    """Attempt to fix unprepared environments, then retry runner startup.

    On success the projects are fully initialized and the function returns normally.
    On any failure it re-raises ``exc``.
    """
    from finecode.wm_server import wm_server as _wm
    from finecode.wm_server.runner import runner_client as rc

    def _notify(message: str, level: str = "ERROR") -> None:
        _wm._notify_all_clients("server/userMessage", {"message": message, "type": level})

    def _runner_status(p: domain.Project) -> rc.RunnerStatus | None:
        r = ws_context.ws_projects_extension_runners.get(p.dir_path, {}).get("dev_workspace")
        return r.status if r is not None else None

    no_venv_projects = [p for p in projects if _runner_status(p) == rc.RunnerStatus.NO_VENV]
    failed_projects = [p for p in projects if _runner_status(p) == rc.RunnerStatus.FAILED]
    # Projects whose tasks were cancelled before save_runner_in_context ran have no runner
    # entry in context at all (_runner_status returns None).  Their venv may or may not
    # exist; install_env_for_project is idempotent and handles both cases.
    # Only CONFIG_VALID projects are expected to have a runner; others (e.g. NO_FINECODE)
    # are intentionally skipped by start_runners_with_presets and must not be auto-repaired.
    no_runner_projects = [
        p for p in projects
        if _runner_status(p) is None
        and p.status == domain.ProjectStatus.CONFIG_VALID
    ]
    affected_projects = no_venv_projects + failed_projects + no_runner_projects

    logger.debug(
        f"_auto_prepare_and_retry: {len(projects)} project(s), statuses: "
        + ", ".join(f"{p.name}={_runner_status(p)}" for p in projects)
    )

    initializing_projects = [p for p in projects if _runner_status(p) == rc.RunnerStatus.INITIALIZING]
    if initializing_projects:
        logger.warning(
            "Projects with runners stuck in INITIALIZING state (will not be auto-repaired): "
            + ", ".join(p.name for p in initializing_projects)
        )

    if not affected_projects:
        _notify(
            f"Starting runners failed: {exc.message}. "  # type: ignore[union-attr]
            f"Did you run `finecode prepare-envs`?"
        )
        raise exc

    affected_names = ", ".join(p.name for p in affected_projects)
    logger.info(f"Environment not prepared for: {affected_names}. Running prepare-envs automatically.")
    _notify(
        f"Environment not prepared for: {affected_names}. Running prepare-envs automatically...",
        level="INFO",
    )

    from finecode.wm_server.services.prepare_envs_service import (
        install_env_for_project,
        PrepareEnvsFailed,
    )

    for project in affected_projects:
        try:
            await install_env_for_project(project, "dev_workspace", ws_context)
        except PrepareEnvsFailed as prep_exc:
            logger.error(f"Auto install_env failed for {project.name}: {prep_exc.message}")
            _notify(f"Auto prepare failed for {project.name}: {prep_exc.message}")
            raise runner_manager.RunnerFailedToStart(
                f"Auto prepare-envs failed for '{project.name}': {prep_exc.message}"
            ) from prep_exc

    # Restart runners so they pick up the newly populated venvs.
    for project in affected_projects:
        try:
            await runner_manager.restart_extension_runner(
                runner_working_dir_path=project.dir_path,
                env_name="dev_workspace",
                ws_context=ws_context,
            )
        except Exception as restart_exc:
            logger.error(
                f"Failed to restart runner for {project.name} after prepare-envs: {restart_exc}"
            )
            _notify(
                f"prepare-envs succeeded but runner restart failed for {project.name}."
            )
            raise exc

    # Some projects may have had their runners start successfully (RUNNING status) but
    # were cancelled in _start_runner_and_read_config after _start_runner finished and
    # before preset resolution and the ws_context.ws_projects upgrade to ResolvedProject
    # could complete.  They are not in affected_projects (their env is fine; restarting
    # them would stop a healthy runner for no reason), but they still need
    # _start_runner_and_read_config to finish.  get_or_start_runner returns them
    # immediately without restarting; only the config-reading tail reruns.
    running_unresolved = [
        p for p in projects if _runner_status(p) == rc.RunnerStatus.RUNNING
    ]

    retry_projects = affected_projects + running_unresolved
    try:
        await runner_manager.start_runners_with_presets(
            projects=retry_projects,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
        )
    except runner_manager.RunnerFailedToStart as retry_exc:
        _notify(f"Runner start failed after auto prepare-envs: {retry_exc.message}")
        raise

    logger.info(f"Auto prepare-envs and runner restart succeeded for: {affected_names}")


async def start_runners_with_auto_prepare(
    projects: list[domain.Project],
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
    python_overrides: dict[str, str] | None = None,
    resolve_presets: bool = True,
) -> None:
    """Start runners for *projects*, auto-preparing environments on failure.

    Wraps ``runner_manager.start_runners_with_presets`` with the same signature.
    On ``RunnerConfigurationError`` (missing venv or package) it runs the relevant
    ``fine_envs`` actions automatically and retries before surfacing the error.
    """
    from finecode.wm_server.runner import runner_manager

    try:
        await runner_manager.start_runners_with_presets(
            projects=projects,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
            python_overrides=python_overrides,
            resolve_presets=resolve_presets,
        )
    except runner_manager.RunnerFailedToStart as exc:
        # Catches both RunnerConfigurationError (NO_VENV detected early) and plain
        # RunnerFailedToStart (venv exists but packages missing → process exits with error).
        # _auto_prepare_and_retry inspects runner statuses and re-raises exc unchanged if
        # no runners are in FAILED/NO_VENV state (i.e. not an env-preparation problem).
        await _auto_prepare_and_retry(
            exc=exc,
            projects=projects,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
        )


async def repair_no_venv_env(
    project: domain.Project,
    env_name: str,
    ws_context: context.WorkspaceContext,
) -> None:
    """Install *env_name* for *project* from scratch and restart its runner.

    Shared by every runner-start call site that needs to react to a
    ``NO_VENV`` runner (venv missing or just wiped because it was found stale
    — see ``finecode_cmd.VenvRelocatedError``) by auto-running
    ``create_envs`` + ``install_envs`` and restarting, instead of surfacing a
    raw "runner failed to start" error to the caller.

    Raises ``prepare_envs_service.PrepareEnvsFailed`` if installation fails;
    propagates whatever the subsequent restart raises otherwise.
    """
    from finecode.wm_server.services.prepare_envs_service import install_env_for_project

    logger.info(
        f"Environment '{env_name}' not prepared for {project.name}. "
        f"Running prepare-envs automatically."
    )
    await install_env_for_project(project, env_name, ws_context)
    await runner_manager.restart_extension_runner(
        runner_working_dir_path=project.dir_path,
        env_name=env_name,
        ws_context=ws_context,
    )
    logger.info(
        f"Auto prepare-envs and runner restart succeeded for env '{env_name}' "
        f"in {project.name}."
    )


async def get_or_start_runner_with_auto_prepare(
    project_def: domain.Project,
    env_name: str,
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
    action_names_to_initialize: list[str] | None = None,
    cmd_override: str | None = None,
) -> runner_client.ExtensionRunnerInfo:
    """Start (or return running) runner for *env_name*, auto-preparing the env if missing.

    Wraps ``runner_manager.get_or_start_runner`` with the same signature.  On
    ``RunnerFailedToStart`` where the runner's status is ``NO_VENV``, installs the env
    via ``install_env_for_project`` and retries.

    Raises ``RunnerFailedToStart`` if the runner cannot start even after auto-prepare,
    or if the failure is not env-related.
    """
    from finecode.wm_server.runner import runner_client as rc

    try:
        return await runner_manager.get_or_start_runner(
            project_def=project_def,
            env_name=env_name,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
            action_names_to_initialize=action_names_to_initialize,
            cmd_override=cmd_override,
        )
    except _jsonrpc_client.ServerFailedToStart as exc:
        runner = ws_context.ws_projects_extension_runners.get(
            project_def.dir_path, {}
        ).get(env_name)
        if runner is None or runner.status != rc.RunnerStatus.NO_VENV:
            raise

        from finecode.wm_server.services.prepare_envs_service import PrepareEnvsFailed

        try:
            await repair_no_venv_env(project_def, env_name, ws_context)
        except PrepareEnvsFailed as prep_exc:
            logger.error(
                f"Auto prepare failed for env '{env_name}' in {project_def.name}: "
                f"{prep_exc.message}"
            )
            raise exc from prep_exc

        return await runner_manager.get_or_start_runner(
            project_def=project_def,
            env_name=env_name,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
            action_names_to_initialize=action_names_to_initialize,
            cmd_override=cmd_override,
        )


async def get_or_start_runners_with_presets(
    project_dir_path: pathlib.Path, ws_context: context.WorkspaceContext
) -> runner_client.ExtensionRunnerInfo:
    """Lazily start the dev_workspace runner for *project_dir_path*, with auto-prepare.

    Returns immediately if the runner is already RUNNING or INITIALIZING (waits for
    the latter).  Uses ``start_runners_with_auto_prepare`` so missing environments are
    fixed automatically before the caller sees an error.

    Returns the ``ExtensionRunnerInfo`` for the dev_workspace runner.
    Raises ``RunnerFailedToStart`` if the runner cannot reach RUNNING status.
    """
    from finecode.wm_server.runner import runner_client, runner_manager

    has_dev_workspace_runner = (
        "dev_workspace" in ws_context.ws_projects_extension_runners.get(project_dir_path, {})
    )
    if not has_dev_workspace_runner:
        project = ws_context.ws_projects[project_dir_path]
        await start_runners_with_auto_prepare([project], ws_context)

    dev_workspace_runner = ws_context.ws_projects_extension_runners[project_dir_path][
        "dev_workspace"
    ]
    if dev_workspace_runner.status == runner_client.RunnerStatus.RUNNING:
        return dev_workspace_runner
    elif dev_workspace_runner.status == runner_client.RunnerStatus.INITIALIZING:
        await dev_workspace_runner.initialized_event.wait()
        return dev_workspace_runner
    elif dev_workspace_runner.status == runner_client.RunnerStatus.REPAIRING:
        if dev_workspace_runner.repair_complete_event is not None:
            await dev_workspace_runner.repair_complete_event.wait()
        dev_workspace_runner = ws_context.ws_projects_extension_runners[project_dir_path]["dev_workspace"]
        return dev_workspace_runner
    else:
        raise runner_manager.RunnerFailedToStart(
            f"Status of dev_workspace runner: {dev_workspace_runner.status}, "
            f"logs: {dev_workspace_runner.logs_path}"
        )
