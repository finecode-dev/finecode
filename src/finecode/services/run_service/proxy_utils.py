from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import typing

import ordered_set
from loguru import logger

from finecode import context, domain, find_project, user_messages
from finecode.runner import runner_manager
from finecode.runner import runner_client
from finecode.runner.runner_manager import RunnerFailedToStart
from finecode.runner.runner_client import RunResultFormat  # reexport

from finecode.services.run_service import payload_preprocessor
from .exceptions import ActionRunFailed, StartingEnvironmentsFailed


async def find_action_project(
    file_path: pathlib.Path, action_name: str, ws_context: context.WorkspaceContext
) -> pathlib.Path:
    try:
        project_path = await find_project.find_project_with_action_for_file(
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
        raise ActionRunFailed(error) from error

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
    params: dict[str, typing.Any],
    ws_context: context.WorkspaceContext,
) -> runner_client.RunActionResponse:
    project_path = await find_action_project(
        file_path=file_path, action_name=action_name, ws_context=ws_context
    )
    project = ws_context.ws_projects[project_path]

    try:
        response = await run_action(
            action_name=action_name,
            params=params,
            project_def=project,
            ws_context=ws_context,
            preprocess_payload=False,
        )
    except ActionRunFailed as exception:
        raise exception

    return response


async def run_action_in_runner(
    action_name: str,
    params: dict[str, typing.Any],
    runner: runner_client.ExtensionRunnerInfo,
    options: dict[str, typing.Any] | None = None,
) -> runner_client.RunActionResponse:
    try:
        response = await runner_client.run_action(
            runner=runner, action_name=action_name, params=params, options=options
        )
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error on running action {action_name}: {error.message}")
        raise ActionRunFailed(error.message)

    return response


class AsyncList[T]:
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
    params: dict[str, typing.Any],
    partial_result_token: int | str,
    runner: runner_client.ExtensionRunnerInfo,
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
    params: dict[str, typing.Any],
    partial_result_token: int | str,
    project_dir_path: pathlib.Path,
    ws_context: context.WorkspaceContext,
) -> collections.abc.AsyncIterator[
    collections.abc.AsyncIterable[domain.PartialResultRawValue]
]:
    logger.trace(f"Run {action_name} in project {project_dir_path}")

    result: AsyncList[domain.PartialResultRawValue] = AsyncList()
    project = ws_context.ws_projects[project_dir_path]
    try:
        async with asyncio.TaskGroup() as tg:
            partial_results_task = tg.create_task(
                get_partial_results(
                    result_list=result, partial_result_token=partial_result_token
                )
            )
            action = next(
                action for action in project.actions if action.name == action_name
            )
            action_envs = ordered_set.OrderedSet(
                [handler.env for handler in action.handlers]
            )
            for env_name in action_envs:
                try:
                    runner = await runner_manager.get_or_start_runner(
                        project_def=project, env_name=env_name, ws_context=ws_context
                    )
                except runner_manager.RunnerFailedToStart as exception:
                    raise ActionRunFailed(
                        f"Runner {env_name} in project {project.dir_path} failed: {exception.message}"
                    ) from exception

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
        errors: list[str] = []
        for exception in eg.exceptions:
            if isinstance(exception, ActionRunFailed):
                errors.append(exception.message)
            else:
                errors.append(str(exception))
                logger.error("Unexpected exception:")
                logger.exception(exception)
        errors_str = ", ".join(errors)
        raise ActionRunFailed(
            f"Run of {action_name} in {project.dir_path} failed: {errors_str}. See logs for more details"
        )


@contextlib.asynccontextmanager
async def find_action_project_and_run_with_partial_results(
    file_path: pathlib.Path,
    action_name: str,
    params: dict[str, typing.Any],
    partial_result_token: int | str,
    ws_context: context.WorkspaceContext,
) -> collections.abc.AsyncIterator[runner_client.RunActionRawResult]:
    logger.trace(f"Run {action_name} on {file_path}")
    project_path = await find_action_project(
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

    try:
        async with asyncio.TaskGroup() as tg:
            # start runners for required environments that aren't already running
            for project_dir_path, required_envs in required_envs_by_project.items():
                project = ws_context.ws_projects[project_dir_path]
                existing_runners = ws_context.ws_projects_extension_runners.get(
                    project_dir_path, {}
                )

                for env_name in required_envs:
                    tg.create_task(
                        _start_runner_or_update_config(
                            env_name=env_name,
                            existing_runners=existing_runners,
                            project=project,
                            update_config_in_running_runners=update_config_in_running_runners,
                            ws_context=ws_context,
                        )
                    )
    except ExceptionGroup as eg:
        errors: list[str] = []
        for exception in eg.exceptions:
            if isinstance(exception, StartingEnvironmentsFailed):
                errors.append(exception.message)
            else:
                errors.append(str(exception))
        raise StartingEnvironmentsFailed(".".join(errors))


async def _start_runner_or_update_config(
    env_name: str,
    existing_runners: dict[str, runner_client.ExtensionRunnerInfo],
    project: domain.Project,
    update_config_in_running_runners: bool,
    ws_context: context.WorkspaceContext,
):
    runner_exist = env_name in existing_runners
    start_runner = True
    if runner_exist:
        runner_is_running = (
            existing_runners[env_name].status == runner_client.RunnerStatus.RUNNING
        )
        start_runner = not runner_is_running

    if start_runner:
        try:
            await runner_manager.start_runner(
                project_def=project, env_name=env_name, ws_context=ws_context
            )
        except runner_manager.RunnerFailedToStart as exception:
            raise StartingEnvironmentsFailed(
                f"Failed to start runner for env '{env_name}' in project '{project.name}': {exception.message}"
            ) from exception
    else:
        if update_config_in_running_runners:
            runner = existing_runners[env_name]
            logger.trace(
                f"Runner {runner.readable_id} is running already, update config"
            )

            try:
                await runner_manager.update_runner_config(
                    runner=runner, project=project
                )
            except RunnerFailedToStart as exception:
                raise StartingEnvironmentsFailed(
                    f"Failed to update config of runner {runner.readable_id}"
                ) from exception


async def run_actions_in_running_project(
    actions: list[str],
    action_payload: dict[str, str],
    project: domain.Project,
    ws_context: context.WorkspaceContext,
    concurrently: bool,
    result_format: RunResultFormat,
) -> dict[str, RunActionResponse]:
    result_by_action: dict[str, RunActionResponse] = {}

    if concurrently:
        run_tasks: list[asyncio.Task] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for action_name in actions:
                    run_task = tg.create_task(
                        run_action(
                            action_name=action_name,
                            params=action_payload,
                            project_def=project,
                            ws_context=ws_context,
                            result_format=result_format,
                        )
                    )
                    run_tasks.append(run_task)
        except ExceptionGroup as eg:
            for exception in eg.exceptions:
                if isinstance(exception, ActionRunFailed):
                    logger.error(f"{exception.message} in {project.name}")
                else:
                    logger.error("Unexpected exception:")
                    logger.exception(exception)
            raise ActionRunFailed(f"Running of actions {actions} failed")

        for idx, run_task in enumerate(run_tasks):
            run_result = run_task.result()
            action_name = actions[idx]
            result_by_action[action_name] = run_result
    else:
        for action_name in actions:
            try:
                run_result = await run_action(
                    action_name=action_name,
                    params=action_payload,
                    project_def=project,
                    ws_context=ws_context,
                    result_format=result_format,
                )
            except ActionRunFailed as exception:
                raise ActionRunFailed(
                    f"Running of action {action_name} failed: {exception.message}"
                )
            except Exception as error:
                logger.error("Unexpected exception")
                logger.exception(error)
                raise ActionRunFailed(
                    f"Running of action {action_name} failed with unexpected exception"
                )

            result_by_action[action_name] = run_result

    return result_by_action


async def run_actions_in_projects(
    actions_by_project: dict[pathlib.Path, list[str]],
    action_payload: dict[str, str],
    ws_context: context.WorkspaceContext,
    concurrently: bool,
    result_format: RunResultFormat,
) -> dict[pathlib.Path, dict[str, RunActionResponse]]:
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
                        result_format=result_format,
                    )
                )
                project_handler_tasks.append(project_task)
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            # TODO: merge all in one?
            raise exception

    results = {}
    projects_paths = list(actions_by_project.keys())
    for idx, project_task in enumerate(project_handler_tasks):
        project_dir_path = projects_paths[idx]
        results[project_dir_path] = project_task.result()

    return results


def find_projects_with_actions(
    ws_context: context.WorkspaceContext, actions: list[str]
) -> dict[pathlib.Path, list[str]]:
    actions_by_project: dict[pathlib.Path, list[str]] = {}
    actions_set = ordered_set.OrderedSet(actions)

    for project in ws_context.ws_projects.values():
        project_actions_names = [action.name for action in project.actions]
        # find which of requested actions are available in the project
        action_to_run_in_project = actions_set & ordered_set.OrderedSet(
            project_actions_names
        )
        relevant_actions_in_project = list(action_to_run_in_project)
        if len(relevant_actions_in_project) > 0:
            actions_by_project[project.dir_path] = relevant_actions_in_project

    return actions_by_project


RunResultFormat = runner_client.RunResultFormat
RunActionResponse = runner_client.RunActionResponse


async def run_action(
    action_name: str,
    params: dict[str, typing.Any],
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    result_format: RunResultFormat = RunResultFormat.JSON,
    preprocess_payload: bool = True,
) -> RunActionResponse:
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action_name} with {formatted_params}")

    if project_def.status != domain.ProjectStatus.CONFIG_VALID:
        raise ActionRunFailed(
            f"Project {project_def.dir_path} has no valid configuration and finecode."
            " Please check logs."
        )

    if preprocess_payload:
        payload = await payload_preprocessor.preprocess_for_project(
            action_name=action_name,
            payload=params,
            project_dir_path=project_def.dir_path,
            ws_context=ws_context,
        )
    else:
        payload = params

    # cases:
    # - base: all action handlers are in one env
    #   -> send `run_action` request to runner in env and let it handle concurrency etc.
    #      It could be done also in workspace manager, but handlers share run context
    # - mixed envs: action handlers are in different envs
    # -- concurrent execution of handlers
    # -- sequential execution of handlers
    assert project_def.actions is not None
    action = next(
        action for action in project_def.actions if action.name == action_name
    )
    all_handlers_envs = ordered_set.OrderedSet(
        [handler.env for handler in action.handlers]
    )
    all_handlers_are_in_one_env = len(all_handlers_envs) == 1

    if all_handlers_are_in_one_env:
        env_name = all_handlers_envs[0]
        response = await _run_action_in_env_runner(
            action_name=action_name,
            payload=payload,
            env_name=env_name,
            project_def=project_def,
            ws_context=ws_context,
            result_format=result_format,
        )
    else:
        # TODO: concurrent vs sequential, this value should be taken from action config
        run_concurrently = False  # action_name == 'lint'
        if run_concurrently:
            ...
            raise NotImplementedError()
        else:
            for handler in action.handlers:
                # TODO: manage run context
                response = await _run_action_in_env_runner(
                    action_name=action_name,
                    payload=payload,
                    env_name=handler.env,
                    project_def=project_def,
                    ws_context=ws_context,
                    result_format=result_format,
                )

    return response


async def _run_action_in_env_runner(
    action_name: str,
    payload: dict[str, typing.Any],
    env_name: str,
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    result_format: RunResultFormat = RunResultFormat.JSON,
):
    try:
        runner = await runner_manager.get_or_start_runner(
            project_def=project_def, env_name=env_name, ws_context=ws_context
        )
    except runner_manager.RunnerFailedToStart as exception:
        raise ActionRunFailed(
            f"Runner {env_name} in project {project_def.dir_path} failed: {exception.message}"
        )

    try:
        response = await runner_client.run_action(
            runner=runner,
            action_name=action_name,
            params=payload,
            options={"result_format": result_format},
        )
    except runner_client.BaseRunnerRequestException as error:
        await user_messages.error(
            f"Action {action_name} failed in {runner.readable_id}: {error.message} . Log file: {runner.logs_path}"
        )
        raise ActionRunFailed(
            f"Action {action_name} failed in {runner.readable_id}: {error.message} . Log file: {runner.logs_path}"
        ) from error

    return response


__all__ = [
    "find_action_project_and_run",
    "find_action_project_and_run_with_partial_results",
    "find_projects_with_actions",
    "find_all_projects_with_action",
    "run_with_partial_results",
    "start_required_environments",
    "run_actions_in_projects",
]
