import asyncio
import pathlib
from typing import NamedTuple

import click
import ordered_set
from loguru import logger

from finecode.workspace_manager import context, domain, services
from finecode.workspace_manager.config import read_configs
from finecode.workspace_manager.runner import manager as runner_manager


class RunFailed(Exception): ...


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

    try:
        try:
            await runner_manager.update_runners(ws_context)
        except runner_manager.RunnerFailedToStart as exception:
            raise RunFailed(
                f"One or more projects are misconfigured, runners for them didn't"
                f" start: {exception.message}. Check logs for details."
            )

        actions_by_projects: dict[pathlib.Path, list[str]] = {}
        if projects_names is not None:
            projects: list[domain.Project] = get_projects_by_names(
                projects_names, ws_context, workdir_path
            )
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
            actions_by_projects = find_projects_with_actions(ws_context, actions)

        return await run_actions_in_all_projects(
            actions_by_projects, action_payload, ws_context, concurrently
        )
    finally:
        services.on_shutdown(ws_context)


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


def find_projects_with_actions(
    ws_context: context.WorkspaceContext, actions: list[str]
) -> dict[pathlib.Path, list[str]]:
    actions_by_project: dict[pathlib.Path, list[str]] = {}
    actions_set = ordered_set.OrderedSet(actions)

    for project in ws_context.ws_projects.values():
        project_actions_names = [action.name for action in project.actions]
        # find which of requested actions are available in the project
        action_to_run_in_project = (
            actions_set & ordered_set.OrderedSet(project_actions_names)
        )
        relevant_actions_in_project = list(action_to_run_in_project)
        if len(relevant_actions_in_project) > 0:
            actions_by_project[project.dir_path] = relevant_actions_in_project

    return actions_by_project


def run_result_to_str(
    run_result: str | dict[str, list[str | dict[str, str | bool]]], action_name: str
) -> str:
    run_result_str = ""
    if isinstance(run_result, str):
        run_result_str = run_result
    elif isinstance(run_result, dict):
        # styled text
        text_parts = run_result.get("parts", [])
        if not isinstance(text_parts, list):
            raise RunFailed(
                f"Running of action {action_name} failed: got unexpected result, 'parts' value expected to be a list."
            )

        for text_part in text_parts:
            if isinstance(text_part, str):
                run_result_str += text_part
            elif isinstance(text_part, dict):
                try:
                    text = text_part["text"]
                except KeyError:
                    raise RunFailed(
                        f"Running of action {action_name} failed: got unexpected result, 'text' value is required in object with styled text params."
                    )

                style_params: dict[str, str | bool] = {}
                if "foreground" in text_part and isinstance(
                    text_part["foreground"], str
                ):
                    style_params["fg"] = text_part["foreground"]

                if "background" in text_part and isinstance(
                    text_part["background"], str
                ):
                    style_params["bg"] = text_part["background"]

                if "bold" in text_part and isinstance(text_part["bold"], bool):
                    style_params["bold"] = text_part["bold"]

                if "underline" in text_part and isinstance(
                    text_part["underline"], bool
                ):
                    style_params["underline"] = text_part["underline"]

                if "overline" in text_part and isinstance(text_part["overline"], bool):
                    style_params["overline"] = text_part["overline"]

                if "italic" in text_part and isinstance(text_part["italic"], bool):
                    style_params["italic"] = text_part["italic"]

                if "blink" in text_part and isinstance(text_part["blink"], bool):
                    style_params["blink"] = text_part["blink"]

                if "strikethrough" in text_part and isinstance(
                    text_part["strikethrough"], bool
                ):
                    style_params["strikethrough"] = text_part["strikethrough"]

                if "reset" in text_part and isinstance(text_part["reset"], bool):
                    style_params["reset"] = text_part["reset"]

                run_result_str += click.style(text, **style_params)
            else:
                raise RunFailed(
                    f"Running of action {action_name} failed: got unexpected result, 'parts' list can contain only strings or objects with styled text."
                )

    return run_result_str


class ActionRunResult(NamedTuple):
    output: str
    return_code: int


async def run_actions_in_running_project(
    actions: list[str],
    action_payload: dict[str, str],
    project: domain.Project,
    ws_context: context.WorkspaceContext,
    concurrently: bool,
) -> dict[str, ActionRunResult]:
    result_by_action: dict[str, ActionRunResult] = {}

    if concurrently:
        run_tasks: list[asyncio.Task] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for action_name in actions:
                    run_task = tg.create_task(
                        services.run_action(
                            action_name=action_name,
                            params=action_payload,
                            project_def=project,
                            ws_context=ws_context,
                            result_format=services.RunResultFormat.STRING,
                        )
                    )
                    run_tasks.append(run_task)
        except ExceptionGroup as eg:
            for error in eg:
                logger.exception(error)
            raise RunFailed(f"Running of action {action_name} failed")

        for idx, run_task in enumerate(run_tasks):
            run_result = run_task.result()
            action_name = actions[idx]
            run_result_str = run_result_to_str(run_result.result, action_name)
            result_by_action[action_name] = ActionRunResult(
                output=run_result_str, return_code=run_result.return_code
            )
    else:
        for action_name in actions:
            try:
                run_result = await services.run_action(
                    action_name=action_name,
                    params=action_payload,
                    project_def=project,
                    ws_context=ws_context,
                    result_format=services.RunResultFormat.STRING,
                )
            except Exception as error:
                logger.exception(error)
                raise RunFailed(f"Running of action {action_name} failed")

            run_result_str = run_result_to_str(run_result.result, action_name)
            result_by_action[action_name] = ActionRunResult(
                output=run_result_str, return_code=run_result.return_code
            )

    return result_by_action


async def run_actions_in_all_projects(
    actions_by_project: dict[pathlib.Path, list[str]],
    action_payload: dict[str, str],
    ws_context: context.WorkspaceContext,
    concurrently: bool,
) -> tuple[str, int]:
    project_handler_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for project_dir_path, actions_to_run in actions_by_project.items():
                project = ws_context.ws_projects[project_dir_path]
                project_task = tg.create_task(
                    run_actions_in_running_project(
                        actions=actions_to_run,
                        action_payload=action_payload,
                        project=project,
                        ws_context=ws_context,
                        concurrently=concurrently,
                    )
                )
                project_handler_tasks.append(project_task)
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            raise exception

    result_output: str = ""
    result_return_code: int = 0

    run_in_many_projects = len(actions_by_project) > 1
    projects_paths = list(actions_by_project.keys())
    for idx, project_task in enumerate(project_handler_tasks):
        project_dir_path = projects_paths[idx]
        result_by_action = project_task.result()
        run_many_actions = len(result_by_action) > 1

        if idx > 0:
            result_output += "\n"

        if run_in_many_projects:
            result_output += f"{click.style(str(project_dir_path), bold=True, underline=True)}\n"

        for action_name, action_result in result_by_action.items():
            if run_many_actions:
                result_output += f"{click.style(action_name, bold=True)}:"
            result_output += action_result.output
            result_return_code |= action_result.return_code

    return (result_output, result_return_code)


__all__ = ["run_actions"]
