import json
import pathlib
import sys
import typing

import ordered_set
from loguru import logger

from finecode.api_server import context, domain
from finecode.api_server.services import run_service, shutdown_service
from finecode.api_server.config import collect_actions, config_models, read_configs
from finecode.api_server.runner import runner_manager

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
    handler_config_overrides: dict[str, dict[str, dict[str, str]]] | None = None,
    save_results: bool = True,
    map_payload_fields: set[str] | None = None,
) -> utils.RunActionsResult:
    ws_context = context.WorkspaceContext([workdir_path])
    if handler_config_overrides:
        ws_context.handler_config_overrides = handler_config_overrides
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
            ) from exception

    try:
        # 1. Start runners with presets to be able to resolve presets. Presets are
        # required to be able to collect all actions, actions handlers and configs.
        try:
            await runner_manager.start_runners_with_presets(projects, ws_context)
        except runner_manager.RunnerFailedToStart as exception:
            raise RunFailed(
                "One or more projects are misconfigured, runners for them didn't"
                + f" start: {exception.message}. Check logs for details."
            ) from exception
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
                actions_by_projects,
                ws_context,
                update_config_in_running_runners=True,
            )
        except run_service.StartingEnvironmentsFailed as exception:
            raise RunFailed(
                f"Failed to start environments for running actions: {exception.message}"
            ) from exception

        payload_overrides_by_project: dict[str, dict[str, typing.Any]] = {}
        if map_payload_fields:
            payload_overrides_by_project = resolve_mapped_payload_fields(
                map_payload_fields=map_payload_fields,
                action_payload=action_payload,
            )

        try:
            return await utils.run_actions_in_projects_and_concat_results(
                actions_by_projects,
                action_payload,
                ws_context,
                concurrently,
                run_trigger=run_service.RunActionTrigger.USER,
                dev_env=run_service.DevEnv.CLI,
                output_json=save_results,
                payload_overrides_by_project=payload_overrides_by_project,
            )
        except run_service.ActionRunFailed as exception:
            raise RunFailed(
                f"Failed to run actions: {exception.message}"
            ) from exception
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
        except StopIteration as exception:
            raise RunFailed(
                f"Project '{projects_names}' not found in working directory '{workdir_path}'"
            ) from exception

        projects.append(project)
    return projects


def resolve_mapped_payload_fields(
    map_payload_fields: set[str],
    action_payload: dict[str, typing.Any],
) -> dict[str, dict[str, typing.Any]]:
    """Resolve mapped payload fields from saved results.

    Returns a dict keyed by project path string, where each value is a dict
    of field overrides for that project.
    """
    results_dir = pathlib.Path(sys.executable).parent.parent / "cache" / "finecode" / "results"
    payload_overrides_by_project: dict[str, dict[str, typing.Any]] = {}

    for field_name in map_payload_fields:
        raw_value = action_payload.get(field_name)
        if raw_value is None:
            raise RunFailed(
                f"Mapped payload field '{field_name}' not found in action payload"
            )

        action_name, field_path = str(raw_value).split(".", 1)
        result_file = results_dir / f"{action_name}.json"
        if not result_file.exists():
            raise RunFailed(
                f"Results file '{result_file}' not found for mapped field '{field_name}'"
            )

        results_by_project: dict[str, typing.Any] = json.loads(result_file.read_text())
        for project_path, project_result in results_by_project.items():
            resolved_value = project_result
            for key in field_path.split("."):
                if not isinstance(resolved_value, dict):
                    raise RunFailed(
                        f"Cannot resolve '{field_path}' in results of '{action_name}' for project '{project_path}'"
                    )
                resolved_value = resolved_value.get(key)

            if project_path not in payload_overrides_by_project:
                payload_overrides_by_project[project_path] = {}
            payload_overrides_by_project[project_path][field_name] = resolved_value

    return payload_overrides_by_project


__all__ = ["run_actions"]
