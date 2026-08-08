"""Configuration recovery — make the configuration on disk take effect.

Callers ask for that outcome and never for a mechanism (ADR-0073). Today the
mechanism is always replacement of the project's runners: a new process cannot
carry stale anything, so its coverage can be stated without conditions. An
in-place update of a running runner is cheaper and only conditionally correct,
and the condition is invisible to the caller.
"""

from __future__ import annotations

import asyncio
import pathlib

from loguru import logger

from finecode.wm_server import context, domain, errors
from finecode.wm_server.config import read_configs
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.services import (
    in_flight_runs,
    next_step,
    runner_start_service,
)


async def reload_config(
    ws_context: context.WorkspaceContext,
    *,
    project_dir: pathlib.Path | None = None,
    all_projects: bool = False,
    rescan: bool = False,
    kill_in_flight_runs: bool = False,
) -> list[dict]:
    """Re-read configuration from disk and replace the runners it configures.

    Returns one result per target project, so a recovery that failed for one of
    them stays attributable.

    Raises:
        ValueError: the target is unstated or doubly stated.
        ConfigurationError: ``rescan`` was asked for and a definition file in the
            workspace is malformed, so the target set cannot be determined.
    """
    if project_dir is not None and all_projects:
        raise ValueError(
            "'project' and 'allProjects' are mutually exclusive — supply the "
            "project path to recover one project, or 'allProjects': true to "
            "recover the whole workspace."
        )
    if project_dir is None and not all_projects:
        raise ValueError(
            "a target is required: pass 'project' with the project path to "
            "recover, or 'allProjects': true to recover the whole workspace."
        )

    if rescan:
        # Projects created since startup are invisible until the workspace dirs
        # are walked again; this is the workspace-scope recovery that replaced
        # `server/reset` (ADR-0076).
        await _rescan_workspace_dirs(ws_context)

    if all_projects:
        target_dirs = [
            project.dir_path
            for project in ws_context.ws_projects.values()
            if project.status is domain.ProjectStatus.CONFIG_VALID
        ]
    else:
        target_dirs = [project_dir]

    if not target_dirs:
        raise ValueError(
            "No project in the workspace has a FineCode configuration to recover"
        )

    results: list[dict] = []
    for target_dir in target_dirs:
        results.append(
            await _reload_project_config(
                target_dir, ws_context, kill_in_flight_runs=kill_in_flight_runs
            )
        )
    return results


async def _rescan_workspace_dirs(ws_context: context.WorkspaceContext) -> None:
    async with ws_context.workspace_state_lock:
        for dir_path in ws_context.ws_dirs_paths:
            await read_configs.read_projects_in_dir(dir_path, ws_context)
        ws_context.ws_editable_packages = (
            read_configs.resolve_workspace_editable_packages(ws_context)
        )


async def _reload_project_config(
    project_dir: pathlib.Path,
    ws_context: context.WorkspaceContext,
    *,
    kill_in_flight_runs: bool,
) -> dict:
    project = ws_context.ws_projects.get(project_dir)
    if project is None:
        return _failure(project_dir, f"Project '{project_dir}' is not in the workspace")

    blocking = in_flight_runs.blocking_runs(
        ws_context, project_dir, kill_in_flight_runs=kill_in_flight_runs
    )
    if blocking:
        return {
            "project": str(project_dir),
            "status": "refused",
            "error": in_flight_runs.refusal_message(project_dir, blocking),
            "inFlight": in_flight_runs.as_json(blocking),
        }

    async with _init_lock(project_dir, ws_context):
        actions_before = _action_names(ws_context.ws_projects.get(project_dir))

        # Every re-read path short-circuits on the presence of this entry — the
        # reason a second addDir for an initialized project does nothing.
        config_in_effect = ws_context.ws_projects_raw_configs.pop(project_dir, None)

        try:
            # Re-read, re-collect and resolve presets *before* the runners are
            # replaced: preset resolution asks the project's running
            # dev_workspace ER where preset packages live, so an implementation
            # that stops the runners first cannot resolve presets at all
            # (ADR-0073, Consequences).
            await runner_start_service.start_runners_with_auto_prepare(
                projects=[project], ws_context=ws_context
            )
            await runner_manager.restart_extension_runners(
                runner_working_dir_path=project_dir, ws_context=ws_context
            )
        except (errors.WmError, runner_manager.RunnerFailedToStart) as exception:
            logger.warning(
                f"Configuration recovery failed for {project_dir}: {exception}"
            )
            failed_env, failed_status = _worst_runner(project_dir, ws_context)
            return _failure(
                project_dir,
                str(exception),
                next_step.for_runner_failure(
                    project_dir,
                    failed_env,
                    failed_status,
                    exception,
                ),
            )
        finally:
            if (
                config_in_effect is not None
                and project_dir not in ws_context.ws_projects_raw_configs
            ):
                # The re-read never got far enough to store one. Leaving the
                # project with no configuration at all would make a failed
                # recovery worse than no recovery: either the configuration on
                # disk is in effect, or the one that already was still is. In a
                # `finally` because every way out has to hold that — including
                # the cancellation a disconnected caller causes, which is not an
                # error anyone reports but leaves the project just as empty.
                ws_context.ws_projects_raw_configs[project_dir] = config_in_effect
            _invalidate_caches(project_dir, ws_context)

        actions_after = _action_names(ws_context.ws_projects.get(project_dir))

    return {
        "project": str(project_dir),
        "status": "recovered",
        "actionsAdded": sorted(actions_after - actions_before),
        "actionsRemoved": sorted(actions_before - actions_after),
    }


def _init_lock(
    project_dir: pathlib.Path, ws_context: context.WorkspaceContext
) -> asyncio.Lock:
    """The lock `addDir` and `startRunners` hold for their slow per-project
    phase, so a recovery and an initialization of the same project cannot run
    against each other (R10)."""
    lock = ws_context.project_init_locks.get(project_dir)
    if lock is None:
        lock = asyncio.Lock()
        ws_context.project_init_locks[project_dir] = lock
    return lock


def _action_names(project: domain.Project | None) -> set[str]:
    if not isinstance(project, domain.CollectedProject):
        return set()
    return {action.name for action in project.actions}


def _failure(project_dir: pathlib.Path, error: str, step: str | None = None) -> dict:
    failure = {"project": str(project_dir), "status": "failed", "error": error}
    if step is not None:
        failure["nextStep"] = step
    return failure


def _worst_runner(
    project_dir: pathlib.Path, ws_context: context.WorkspaceContext
) -> tuple[str | None, domain.ExtensionRunnerStatus | None]:
    """The runner that explains the failure, if any is in a state that does.

    A project has several runners and the recovery failed for the project, so
    the reportable state is the one of them that is in a state a caller can act
    on — reported with its environment, because the command that fixes it takes
    one.
    """
    runners = ws_context.ws_projects_extension_runners.get(project_dir, {})
    for env_name, runner in runners.items():
        if runner.status is domain.ExtensionRunnerStatus.NO_VENV:
            return env_name, runner.status
    return None, None


def _invalidate_caches(
    project_dir: pathlib.Path, ws_context: context.WorkspaceContext
) -> None:
    """Drop every cached answer that was derived from this project's actions.

    These caches carry no correctness guarantee past their last invalidation, so
    one missed entry is a stale action route that survives the operation meant to
    refresh it.
    """
    ws_context.ws_action_schemas.pop(project_dir, None)

    for node_id, cached in list(ws_context.cached_actions_by_id.items()):
        if cached.project_path == project_dir:
            del ws_context.cached_actions_by_id[node_id]

    for dir_str, project_by_action in list(
        ws_context.project_path_by_dir_and_action.items()
    ):
        # A directory inside the project may now resolve to it (or stop doing
        # so), which is not visible from the cached value alone.
        if pathlib.Path(dir_str).is_relative_to(project_dir):
            del ws_context.project_path_by_dir_and_action[dir_str]
            continue
        for action_name, cached_dir in list(project_by_action.items()):
            if cached_dir == project_dir:
                del project_by_action[action_name]
