import asyncio
import pathlib
from typing import NamedTuple

import click
import ordered_set
from loguru import logger

from finecode import context, domain
from finecode.services import run_service, shutdown_service
from finecode.config import collect_actions, config_models, read_configs
from finecode.runner import manager as runner_manager

from finecode.cli_app import utils


class RunFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def run_actions(
    workdir_path: pathlib.Path,
    projects_names: list[str] | None,
    actions: list[str],
    action_payload: dict[str, str],
    concurrently: bool,
) -> tuple[str, int]:
    ws_context = context.WorkspaceContext([workdir_path])
    await read_configs.read_projects_in_dir(
        dir_path=workdir_path, ws_context=ws_context
    )

    if projects_names is not None:
        # projects are provided. Filter out other projects if there are more, they would
        # not be used (run can be started in a workspace with also other projects)
        ws_context.ws_projects = {
            project_dir_path: project
            for project_dir_path, project in ws_context.ws_projects.items()
            if project.name in projects_names
        }

        # make sure all projects use finecode
        config_problem_found = False
        for project in ws_context.ws_projects.values():
            if project.status != domain.ProjectStatus.CONFIG_VALID:
                if project.status == domain.ProjectStatus.NO_FINECODE:
                    logger.error(
                        f"You asked to run action in project '{project.name}', but finecode is not used in it(=there is no 'dev_workspace' environment with 'finecode' package in it)"
                    )
                    config_problem_found = True
                elif project.status == domain.ProjectStatus.CONFIG_INVALID:
                    logger.error(
                        f"You asked to run action in project '{project.name}', but its configuration is invalid(see logs above for more details)"
                    )
                    config_problem_found = True
                else:
                    logger.error(
                        f"You asked to run action in project '{project.name}', but it has unexpected status: {project.status}"
                    )
                    config_problem_found = True

        if config_problem_found:
            raise RunFailed(
                "There is a problem with configuration. See previous messages for more details"
            )
    else:
        # filter out packages that don't use finecode
        ws_context.ws_projects = {
            project_dir_path: project
            for project_dir_path, project in ws_context.ws_projects.items()
            if project.status != domain.ProjectStatus.NO_FINECODE
        }

        # check that configuration of packages that use finecode is valid
        config_problem_found = False
        for project in ws_context.ws_projects.values():
            if project.status == domain.ProjectStatus.CONFIG_VALID:
                continue
            elif project.status == domain.ProjectStatus.CONFIG_INVALID:
                logger.error(
                    f"Project '{project.name}' has invalid config, see messages above for more details"
                )
                config_problem_found = True
            else:
                logger.error(
                    f"Project '{project.name}' has unexpected status: {project.status}"
                )
                config_problem_found = True

        if config_problem_found:
            raise RunFailed(
                "There is a problem with configuration. See previous messages for more details"
            )

    projects: list[domain.Project] = []
    if projects_names is not None:
        projects = get_projects_by_names(projects_names, ws_context, workdir_path)
    else:
        projects = list(ws_context.ws_projects.values())

    # first read configs without presets to be able to start runners with presets
    for project in projects:
        try:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context, resolve_presets=False
            )
            collect_actions.collect_actions(
                project_path=project.dir_path, ws_context=ws_context
            )
        except config_models.ConfigurationError as exception:
            raise RunFailed(
                f"Reading project config and collecting actions in {project.dir_path} failed: {exception.message}"
            )

    try:
        # 1. Start runners with presets to be able to resolve presets. Presets are
        # required to be able to collect all actions, actions handlers and configs.
        try:
            await runner_manager.start_runners_with_presets(projects, ws_context)
        except runner_manager.RunnerFailedToStart as exception:
            raise RunFailed(
                f"One or more projects are misconfigured, runners for them didn't"
                f" start: {exception.message}. Check logs for details."
            )
        except Exception as exception:
            logger.error("Unexpected exception:")
            logger.exception(exception)

        actions_by_projects: dict[pathlib.Path, list[str]] = {}
        if projects_names is not None:
            # check that all projects have all actions to detect problem and provide
            # feedback as early as possible
            actions_set: ordered_set.OrderedSet[str] = ordered_set.OrderedSet(actions)
            for project in projects:
                project_actions_set: ordered_set.OrderedSet[str] = (
                    ordered_set.OrderedSet([action.name for action in project.actions])
                )
                missing_actions = actions_set - project_actions_set
                if len(missing_actions) > 0:
                    raise RunFailed(
                        f"Actions {', '.join(missing_actions)} not found in project '{project.name}'"
                    )
                actions_by_projects[project.dir_path] = actions
        else:
            # no explicit project, run in `workdir`, it's expected to be a ws dir and
            # actions will be run in all projects inside
            actions_by_projects = run_service.find_projects_with_actions(
                ws_context, actions
            )

        try:
            await run_service.start_required_environments(
                actions_by_projects, ws_context, update_config_in_running_runners=True
            )
        except run_service.StartingEnvironmentsFailed as exception:
            raise RunFailed(
                f"Failed to start environments for running actions: {exception.message}"
            )

        try:
            return await utils.run_actions_in_projects_and_concat_results(
                actions_by_projects, action_payload, ws_context, concurrently
            )
        except run_service.ActionRunFailed as exception:
            raise RunFailed(f"Failed to run actions: {exception.message}")
    finally:
        shutdown_service.on_shutdown(ws_context)


def get_projects_by_names(
    projects_names: list[str],
    ws_context: context.WorkspaceContext,
    workdir_path: pathlib.Path,
) -> list[domain.Project]:
    projects: list[domain.Project] = []
    for project_name in projects_names:
        try:
            project = next(
                project
                for project in ws_context.ws_projects.values()
                if project.name == project_name
            )
        except StopIteration:
            raise RunFailed(
                f"Project '{projects_names}' not found in working directory '{workdir_path}'"
            )

        projects.append(project)
    return projects


__all__ = ["run_actions"]
