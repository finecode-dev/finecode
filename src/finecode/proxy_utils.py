import asyncio
import collections.abc
import contextlib
import pathlib
from typing import Any

import ordered_set
from loguru import logger

from finecode import context, domain, find_project, services
from finecode.runner import manager as runner_manager
from finecode.runner import runner_client, runner_info
from finecode.runner.manager import RunnerFailedToStart
from finecode.services import ActionRunFailed


def find_action_project(
    file_path: pathlib.Path, action_name: str, ws_context: context.WorkspaceContext
) -> pathlib.Path:
    try:
        project_path = find_project.find_project_with_action_for_file(
            file_path=file_path,
            action_name=action_name,
            ws_context=ws_context,
        )
    except find_project.FileNotInWorkspaceException as error:
        raise error
    except find_project.FileHasNotActionException as error:
        raise error
    except ValueError as error:
        logger.warning(f"Skip {action_name} on {file_path}: {error}")
        raise ActionRunFailed(error)

    project_status = ws_context.ws_projects[project_path].status
    if project_status != domain.ProjectStatus.CONFIG_VALID:
        logger.info(
            f"Extension runner {project_path} has no valid config with finecode, "
            f"status: {project_status.name}"
        )
        raise ActionRunFailed(
            f"Project {project_path} has no valid config with finecode,"
            f"status: {project_status.name}"
        )

    return project_path


async def find_action_project_and_run(
    file_path: pathlib.Path,
    action_name: str,
    params: dict[str, Any],
    ws_context: context.WorkspaceContext,
) -> runner_client.RunActionResponse:
    project_path = find_action_project(
        file_path=file_path, action_name=action_name, ws_context=ws_context
    )
    project = ws_context.ws_projects[project_path]

    try:
        response = await services.run_action(
            action_name=action_name,
            params=params,
            project_def=project,
            ws_context=ws_context,
            preprocess_payload=False,
        )
    except services.ActionRunFailed as exception:
        raise exception

    return response


async def run_action_in_runner(
    action_name: str,
    params: dict[str, Any],
    runner: runner_info.ExtensionRunnerInfo,
    options: dict[str, Any] | None = None,
) -> runner_client.RunActionResponse:
    try:
        response = await runner_client.run_action(
            runner=runner, action_name=action_name, params=params, options=options
        )
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error on running action {action_name}: {error.message}")
        raise ActionRunFailed(error.message)

    return response


class AsyncList[T]():
    def __init__(self) -> None:
        self.data: list[T] = []
        self.change_event: asyncio.Event = asyncio.Event()
        self.ended: bool = False

    def append(self, el: T) -> None:
        self.data.append(el)
        self.change_event.set()

    def end(self) -> None:
        self.ended = True
        self.change_event.set()

    def __aiter__(self) -> collections.abc.AsyncIterator[T]:
        return AsyncListIterator(self)


class AsyncListIterator[T](collections.abc.AsyncIterator[T]):
    def __init__(self, async_list: AsyncList[T]):
        self.async_list = async_list
        self.current_index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> T:
        if len(self.async_list.data) <= self.current_index:
            if self.async_list.ended:
                # already ended
                raise StopAsyncIteration()

            # not ended yet, wait for the next change
            await self.async_list.change_event.wait()
            self.async_list.change_event.clear()
            if self.async_list.ended:
                # the last change ended the list
                raise StopAsyncIteration()

        self.current_index += 1
        return self.async_list.data[self.current_index - 1]


async def run_action_and_notify(
    action_name: str,
    params: dict[str, Any],
    partial_result_token: int | str,
    runner: runner_info.ExtensionRunnerInfo,
    result_list: AsyncList,
    partial_results_task: asyncio.Task,
) -> runner_client.RunActionResponse:
    try:
        return await run_action_in_runner(
            action_name=action_name,
            params=params,
            runner=runner,
            options={"partial_result_token": partial_result_token},
        )
    finally:
        result_list.end()
        partial_results_task.cancel("Got final result")


async def get_partial_results(
    result_list: AsyncList, partial_result_token: int | str
) -> None:
    try:
        with runner_manager.partial_results.iterator() as iterator:
            async for partial_result in iterator:
                if partial_result.token == partial_result_token:
                    result_list.append(partial_result.value)
    except asyncio.CancelledError:
        pass


@contextlib.asynccontextmanager
async def run_with_partial_results(
    action_name: str,
    params: dict[str, Any],
    partial_result_token: int | str,
    project_dir_path: pathlib.Path,
    ws_context: context.WorkspaceContext,
) -> collections.abc.AsyncIterator[
    collections.abc.AsyncIterable[domain.PartialResultRawValue]
]:
    logger.trace(f"Run {action_name} in project {project_dir_path}")

    result: AsyncList[domain.PartialResultRawValue] = AsyncList()
    try:
        async with asyncio.TaskGroup() as tg:
            partial_results_task = tg.create_task(
                get_partial_results(
                    result_list=result, partial_result_token=partial_result_token
                )
            )
            project = ws_context.ws_projects[project_dir_path]
            action = next(action for action in project.actions if action.name == "lint")
            action_envs = ordered_set.OrderedSet(
                [handler.env for handler in action.handlers]
            )
            runners_by_env = ws_context.ws_projects_extension_runners[project_dir_path]
            for env in action_envs:
                runner = runners_by_env[env]
                tg.create_task(
                    run_action_and_notify(
                        action_name=action_name,
                        params=params,
                        partial_result_token=partial_result_token,
                        runner=runner,
                        result_list=result,
                        partial_results_task=partial_results_task,
                    )
                )

            yield result
    except ExceptionGroup as eg:
        for exc in eg.exceptions:
            logger.exception(exc)
        raise ActionRunFailed(eg)


@contextlib.asynccontextmanager
async def find_action_project_and_run_with_partial_results(
    file_path: pathlib.Path,
    action_name: str,
    params: dict[str, Any],
    partial_result_token: int | str,
    ws_context: context.WorkspaceContext,
) -> collections.abc.AsyncIterator[runner_client.RunActionRawResult]:
    logger.trace(f"Run {action_name} on {file_path}")
    project_path = find_action_project(
        file_path=file_path, action_name=action_name, ws_context=ws_context
    )
    return run_with_partial_results(
        action_name=action_name,
        params=params,
        partial_result_token=partial_result_token,
        project_dir_path=project_path,
        ws_context=ws_context,
    )


def find_all_projects_with_action(
    action_name: str, ws_context: context.WorkspaceContext
) -> list[pathlib.Path]:
    projects = ws_context.ws_projects
    relevant_projects: dict[pathlib.Path, domain.Project] = {
        path: project
        for path, project in projects.items()
        if project.status != domain.ProjectStatus.NO_FINECODE
    }

    # exclude projects without valid config and projects without requested action
    for project_dir_path, project_def in relevant_projects.copy().items():
        if project_def.status != domain.ProjectStatus.CONFIG_VALID:
            # projects without valid config have no actions. Files of those projects
            # will be not processed because we don't know whether it has one of expected
            # actions
            continue

        # all running projects have actions
        assert project_def.actions is not None

        try:
            next(action for action in project_def.actions if action.name == action_name)
        except StopIteration:
            del relevant_projects[project_dir_path]
            continue

    relevant_projects_paths: list[pathlib.Path] = list(relevant_projects.keys())
    return relevant_projects_paths


class StartingEnvironmentsFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def start_required_environments(
    actions_by_projects: dict[pathlib.Path, list[str]],
    ws_context: context.WorkspaceContext,
    update_config_in_running_runners: bool = False,
) -> None:
    """Collect all required envs from actions that will be run and start them."""
    required_envs_by_project: dict[pathlib.Path, set[str]] = {}
    for project_dir_path, action_names in actions_by_projects.items():
        project = ws_context.ws_projects[project_dir_path]
        if project.actions is not None:
            project_required_envs = set()
            for action_name in action_names:
                # find the action and collect envs from its handlers
                action = next(
                    (a for a in project.actions if a.name == action_name), None
                )
                if action is not None:
                    for handler in action.handlers:
                        project_required_envs.add(handler.env)
            required_envs_by_project[project_dir_path] = project_required_envs

    # start runners for required environments that aren't already running
    for project_dir_path, required_envs in required_envs_by_project.items():
        project = ws_context.ws_projects[project_dir_path]
        existing_runners = ws_context.ws_projects_extension_runners.get(
            project_dir_path, {}
        )

        for env_name in required_envs:
            runner_exist = env_name in existing_runners
            start_runner = True
            if runner_exist:
                runner_is_running = (
                    existing_runners[env_name].status
                    == runner_info.RunnerStatus.RUNNING
                )
                start_runner = not runner_is_running

            if start_runner:
                try:
                    runner = await runner_manager.start_runner(
                        project_def=project, env_name=env_name, ws_context=ws_context
                    )
                except runner_manager.RunnerFailedToStart as e:
                    raise StartingEnvironmentsFailed(
                        f"Failed to start runner for env '{env_name}' in project '{project.name}': {e}"
                    )
            else:
                if update_config_in_running_runners:
                    runner = existing_runners[env_name]
                    logger.trace(
                        f"Runner {runner.working_dir_path} {runner.env_name} is running already, update config"
                    )

                    try:
                        await runner_manager.update_runner_config(
                            runner=runner, project=project
                        )
                    except RunnerFailedToStart as exception:
                        raise StartingEnvironmentsFailed(
                            f"Failed to update config of runner {runner.working_dir_path} {runner.env_name}"
                        )


__all__ = [
    "find_action_project_and_run",
    "find_action_project_and_run_with_partial_results",
    "run_with_partial_results",
    # reexport for easier use of proxy helpers
    "ActionRunFailed",
    "start_required_environments",
    "StartingEnvironmentsFailed",
]
