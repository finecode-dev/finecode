"""Server-side orchestration for prepare-envs.

Public API
----------
- ``prepare_envs``: full workspace environment preparation (equivalent to the
  ``prepare-envs`` CLI command), runs server-side without requiring a client.
- ``install_env_for_project``: targeted repair — installs a single named env
  for a project via its dev_workspace runner.

Both functions raise :class:`PrepareEnvsFailed` on failure.
"""
from __future__ import annotations

import asyncio
import pathlib

from loguru import logger

from finecode.wm_server import context, domain


class PrepareEnvsFailed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _run_env_action(
    action_source: str,
    params: dict,
    executor_project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> str | None:
    """Run a ``fine_envs`` action on *executor_project*'s dev_workspace runner.

    Returns an error string on failure, ``None`` on success.
    """
    from finecode.wm_server.runner import runner_client as rc
    from finecode.wm_server.services import run_service

    action = next((a for a in executor_project.actions if a.source == action_source), None)
    if action is None or action.canonical_source is None:
        return f"{action_source} not available in project '{executor_project.name}'"

    try:
        result = await run_service.ProjectExecutor(ws_context).run_action(
            action_source=action.canonical_source,
            params=params,
            project_path=executor_project.dir_path,
            run_trigger=rc.RunActionTrigger.USER,
            dev_env=rc.DevEnv.CLI,
            result_formats=[rc.RunResultFormat.STRING],
            initialize_all_handlers=True,
        )
    except run_service.ActionRunFailed as action_exc:
        return action_exc.message

    if result.return_code != 0:
        return (result.result_by_format or {}).get("string", "") or f"{action_source} failed"

    return None


async def prepare_envs(
    ws_context: context.WorkspaceContext,
    workdir_path: pathlib.Path,
    recreate: bool = False,
    env_names: list[str] | None = None,
    project_names: list[str] | None = None,
) -> None:
    """Prepare all virtual environments for a workspace.

    Server-side equivalent of the ``prepare-envs`` CLI command. Orchestrates:
    1. Project discovery.
    2. Check / remove dev_workspace environments.
    2.5. Start workspace root dev_workspace runner.
    3. create_envs + install_envs for subproject dev_workspace envs.
    4. Start all dev_workspace runners.
    5. create_envs across all projects.
    6. install_envs across all projects (optionally filtered by env_names).

    Args:
        ws_context: Workspace context.
        workdir_path: Absolute path to the workspace root directory.
        recreate: When True, delete and recreate all dev_workspace venvs.
        env_names: Limit install_envs (step 6) to these env names. create_envs
            (step 5) still runs for all envs.
        project_names: Limit steps 3, 5, and 6 to these projects.

    Raises:
        PrepareEnvsFailed: if any step fails.
    """
    from finecode.wm_server.config import read_configs
    from finecode.wm_server.runner import runner_manager
    from finecode.wm_server.services import runner_start_service

    # Step 1 — Discover projects.
    logger.info("Discovering projects...")
    if workdir_path not in ws_context.ws_dirs_paths:
        ws_context.ws_dirs_paths.append(workdir_path)
        await read_configs.read_projects_in_dir(workdir_path, ws_context)

    for project in list(ws_context.ws_projects.values()):
        if (
            project.dir_path.is_relative_to(workdir_path)
            and project.dir_path not in ws_context.ws_projects_raw_configs
        ):
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=False
            )

    ws_context.ws_editable_packages = read_configs.resolve_workspace_editable_packages(ws_context)

    workdir_project = ws_context.ws_projects.get(workdir_path)
    if workdir_project is None:
        raise PrepareEnvsFailed(
            "prepare-envs can be run only from workspace/project root"
        )

    projects = [
        p
        for p in ws_context.ws_projects.values()
        if p.dir_path.is_relative_to(workdir_path)
    ]

    invalid_projects = [
        p for p in projects if p.status == domain.ProjectStatus.CONFIG_INVALID
    ]
    if invalid_projects:
        names = [p.name for p in invalid_projects]
        raise PrepareEnvsFailed(f"Projects have invalid configuration: {names}")

    other_projects = [
        p
        for p in projects
        if p.dir_path != workdir_path and p.status == domain.ProjectStatus.CONFIG_VALID
    ]

    project_paths_filter: list[str] | None = None
    if project_names is not None:
        unknown = [
            n for n in project_names if not any(p.name == n for p in projects)
        ]
        if unknown:
            raise PrepareEnvsFailed(f"Unknown project(s): {unknown}")
        other_projects = [p for p in other_projects if p.name in project_names]
        project_paths_filter = [
            str(p.dir_path) for p in projects if p.name in project_names
        ]

    logger.info(f"Found {len(projects)} project(s): {[p.name for p in projects]}")

    # Step 2 — Check / remove dev_workspace envs.
    logger.info("Checking dev workspace environments...")

    async def _check_or_remove(project: domain.Project) -> None:
        if recreate:
            logger.trace(f"Recreating dev_workspace for '{project.name}'")
            runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
            runner = runners.get("dev_workspace")
            if runner is not None:
                await runner_manager.stop_extension_runner(runner=runner)
            runner_manager.remove_runner_env(project.dir_path, "dev_workspace")
        else:
            valid = await runner_manager.check_runner(
                runner_dir=project.dir_path, env_name="dev_workspace"
            )
            if not valid:
                logger.warning(
                    f"Env 'dev_workspace' in project '{project.name}' is invalid,"
                    " recreating it"
                )
                runners = ws_context.ws_projects_extension_runners.get(
                    project.dir_path, {}
                )
                runner = runners.get("dev_workspace")
                if runner is not None:
                    await runner_manager.stop_extension_runner(runner=runner)
                runner_manager.remove_runner_env(project.dir_path, "dev_workspace")

    try:
        async with asyncio.TaskGroup() as tg:
            for project in other_projects:
                tg.create_task(_check_or_remove(project))
    except* PrepareEnvsFailed as eg:
        raise eg.exceptions[0]
    except* Exception as eg:
        raise PrepareEnvsFailed(
            f"Failed to check/remove environments: {eg.exceptions[0]}"
        ) from eg.exceptions[0]

    # Step 2.5 — Start workspace root dev_workspace runner.
    if other_projects:
        logger.info("Starting workspace root dev_workspace runner...")
        try:
            await runner_start_service.start_runners_with_auto_prepare(
                projects=[workdir_project],
                ws_context=ws_context,
                initialize_all_handlers=True,
            )
        except Exception as exc:
            raise PrepareEnvsFailed(
                f"Starting workspace root runner failed: {exc}"
            ) from exc

    # Step 3 — create_envs + install_envs for subproject dev_workspace envs.
    logger.info("Creating/updating dev workspace environments...")
    root_project = ws_context.ws_projects.get(workdir_path)
    dw_envs = [
        {
            "name": "dev_workspace",
            "venv_dir_path": (p.dir_path / ".venvs" / "dev_workspace").as_uri(),
            "project_def_path": (p.dir_path / "pyproject.toml").as_uri(),
        }
        for p in other_projects
    ]
    if dw_envs and isinstance(root_project, domain.CollectedProject):
        error = await _run_env_action(
            "fine_envs.CreateEnvsAction", {"envs": dw_envs}, root_project, ws_context
        )
        if error:
            raise PrepareEnvsFailed(f"dev_workspace create_envs failed: {error}")

        error = await _run_env_action(
            "fine_envs.InstallEnvsAction", {"envs": dw_envs}, root_project, ws_context
        )
        if error:
            raise PrepareEnvsFailed(f"dev_workspace install_envs failed: {error}")
    elif not dw_envs:
        logger.info("No dev_workspace environments to bootstrap, skipping")

    # Step 4 — Start all dev_workspace runners.
    logger.info("Starting dev_workspace runners...")
    projects_to_start: list[domain.Project]
    if project_paths_filter is not None:
        projects_to_start = [
            p
            for p in ws_context.ws_projects.values()
            if str(p.dir_path) in project_paths_filter
        ]
    else:
        projects_to_start = [
            p
            for p in ws_context.ws_projects.values()
            if p.dir_path.is_relative_to(workdir_path)
            and p.status == domain.ProjectStatus.CONFIG_VALID
        ]
    try:
        await runner_start_service.start_runners_with_auto_prepare(
            projects=projects_to_start,
            ws_context=ws_context,
        )
    except Exception as exc:
        raise PrepareEnvsFailed(f"Starting runners failed: {exc}") from exc

    # Step 5 — create_envs across all projects.
    logger.info("Creating envs...")
    step_projects = [
        p
        for p in ws_context.ws_projects.values()
        if isinstance(p, domain.CollectedProject)
        and p.dir_path.is_relative_to(workdir_path)
        and (project_paths_filter is None or str(p.dir_path) in project_paths_filter)
    ]

    create_errors: list[str] = []

    async def _create_one(p: domain.CollectedProject) -> None:
        err = await _run_env_action("fine_envs.CreateEnvsAction", {}, p, ws_context)
        if err:
            create_errors.append(err)

    install_errors: list[str] = []

    async def _install_one(p: domain.CollectedProject) -> None:
        params: dict = {}
        if env_names is not None:
            params["env_names"] = env_names
        err = await _run_env_action("fine_envs.InstallEnvsAction", params, p, ws_context)
        if err:
            install_errors.append(err)

    await asyncio.gather(*[_create_one(p) for p in step_projects])
    if create_errors:
        raise PrepareEnvsFailed("'create_envs' failed:\n" + "\n".join(create_errors))

    # Step 6 — install_envs across all projects.
    logger.info("Installing dependencies...")
    await asyncio.gather(*[_install_one(p) for p in step_projects])
    if install_errors:
        raise PrepareEnvsFailed("'install_envs' failed:\n" + "\n".join(install_errors))


async def install_env_for_project(
    project: domain.Project,
    env_name: str,
    ws_context: context.WorkspaceContext,
) -> None:
    """Install a specific environment for a project.

    Routing:
    - ``dev_workspace`` envs: delegated to the **workspace root's** dev_workspace runner,
      because the subproject's own runner does not exist yet.
    - All other envs: delegated to the **subproject's own** dev_workspace runner, which
      must be startable by the time a non-dev_workspace env is needed.

    In both cases the executor runner is started (or confirmed running) before the
    ``CreateEnvsAction`` + ``InstallEnvsAction`` pair is invoked.

    Args:
        project: The project whose environment needs to be installed.
        env_name: The name of the environment to install (e.g. ``"dev_no_runtime"``
            or ``"dev_workspace"``).
        ws_context: Workspace context.

    Raises:
        PrepareEnvsFailed: if the environment cannot be installed.
    """
    from finecode.wm_server.services import runner_start_service

    root_dir = ws_context.ws_dirs_paths[0]

    if env_name == "dev_workspace":
        # dev_workspace bootstrap: the subproject's own runner doesn't exist yet, so
        # delegate to the workspace root's runner.
        if project.dir_path == root_dir:
            # Cannot install the root project's dev_workspace via itself — circular.
            # Non-dev_workspace envs on the root project are safe because by then the
            # root dev_workspace runner is already running.
            venv_path = project.dir_path / ".venvs" / "dev_workspace"
            if not venv_path.exists():
                detail = f"venv directory does not exist ({venv_path})"
            else:
                runner = (
                    ws_context.ws_projects_extension_runners
                    .get(project.dir_path, {})
                    .get("dev_workspace")
                )
                if runner is not None and runner.logs_path is not None:
                    detail = (
                        f"venv exists but runner failed to start "
                        f"(status: {runner.status.name}, logs: {runner.logs_path})"
                    )
                else:
                    detail = "venv exists but runner could not start"
            raise PrepareEnvsFailed(
                f"Cannot auto-install env 'dev_workspace' for the workspace root project: "
                f"{detail}. Run `finecode prepare-envs` to set up the environment."
            )

        root_project = ws_context.ws_projects.get(root_dir)
        if root_project is None:
            raise PrepareEnvsFailed(
                f"Root project not found — cannot install dev_workspace for '{project.name}'"
            )

        # Serialize concurrent root-runner initializations so that canonical_source
        # values are populated before the env-creation calls below.
        # Use env_install_locks (not project_init_locks) to avoid deadlocking
        # with _handle_add_dir / _handle_start_runners, which hold project_init_locks
        # for the entire slow startup phase and call into this function indirectly
        # via _auto_prepare_and_retry.
        root_init_lock = ws_context.env_install_locks.get(root_dir)
        if root_init_lock is None:
            root_init_lock = asyncio.Lock()
            ws_context.env_install_locks[root_dir] = root_init_lock

        async with root_init_lock:
            root_project = ws_context.ws_projects.get(root_dir)
            try:
                await runner_start_service.start_runners_with_auto_prepare(
                    [root_project], ws_context
                )
                root_project = ws_context.ws_projects.get(root_dir)
            except Exception as exc:
                raise PrepareEnvsFailed(
                    f"Root project runner not available — cannot install dev_workspace"
                    f" for '{project.name}': {exc}"
                ) from exc

            if not isinstance(root_project, domain.CollectedProject):
                raise PrepareEnvsFailed(
                    f"Root project not ready — cannot install dev_workspace for '{project.name}'"
                )

        executor_project: domain.CollectedProject = root_project
    else:
        # Non-dev_workspace env: use the subproject's own dev_workspace runner.
        # Its dev_workspace must already exist (it was set up before any other envs
        # are created), so starting it here is safe and idempotent.
        project_init_lock = ws_context.env_install_locks.get(project.dir_path)
        if project_init_lock is None:
            project_init_lock = asyncio.Lock()
            ws_context.env_install_locks[project.dir_path] = project_init_lock

        async with project_init_lock:
            try:
                await runner_start_service.start_runners_with_auto_prepare(
                    [project], ws_context
                )
            except Exception as exc:
                raise PrepareEnvsFailed(
                    f"Project runner for '{project.name}' not available — cannot install"
                    f" env '{env_name}': {exc}"
                ) from exc
            resolved = ws_context.ws_projects.get(project.dir_path)

        if not isinstance(resolved, domain.CollectedProject):
            raise PrepareEnvsFailed(
                f"Project '{project.name}' not ready — cannot install env '{env_name}'"
            )
        executor_project = resolved

    env_spec = {
        "name": env_name,
        "venv_dir_path": (project.dir_path / ".venvs" / env_name).as_uri(),
        "project_def_path": (project.dir_path / "pyproject.toml").as_uri(),
    }

    # Create the venv if it does not exist yet (idempotent on existing venvs).
    error = await _run_env_action(
        "fine_envs.CreateEnvsAction", {"envs": [env_spec]}, executor_project, ws_context
    )
    if error:
        raise PrepareEnvsFailed(
            f"create_envs failed for env '{env_name}' in '{project.name}': {error}"
        )

    error = await _run_env_action(
        "fine_envs.InstallEnvsAction", {"envs": [env_spec]}, executor_project, ws_context
    )
    if error:
        raise PrepareEnvsFailed(
            f"install_envs failed for env '{env_name}' in '{project.name}': {error}"
        )


__all__ = ["PrepareEnvsFailed", "prepare_envs", "install_env_for_project"]
