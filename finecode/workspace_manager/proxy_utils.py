import asyncio
import collections.abc
import contextlib
from pathlib import Path
from typing import Any

from loguru import logger

from finecode.workspace_manager import context, domain, find_project
from finecode.workspace_manager.runner import manager as runner_manager
from finecode.workspace_manager.runner import runner_client, runner_info


class ActionRunFailed(Exception): ...


def find_action_project_runner(
    file_path: Path, action_name: str, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo:
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
    if project_status != domain.ProjectStatus.RUNNING:
        logger.info(
            f"Extension runner {project_path} is not running, "
            f"status: {project_status.name}"
        )
        raise ActionRunFailed(
            f"Extension runner {project_path} is not running, "
            f"status: {project_status.name}"
        )

    runner = ws_context.ws_projects_extension_runners[project_path]
    return runner


async def find_action_project_and_run(
    file_path: Path,
    action_name: str,
    params: dict[str, Any],
    ws_context: context.WorkspaceContext,
) -> runner_client.RunActionResponse:
    runner = find_action_project_runner(
        file_path=file_path, action_name=action_name, ws_context=ws_context
    )
    try:
        response = await runner_client.run_action(
            runner=runner, action_name=action_name, params=params
        )
    except runner_client.BaseRunnerRequestException as error:
        logger.error(f"Error on running action {action_name} on {file_path}: {error.message}")
        raise ActionRunFailed(error.message)

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
) -> None:
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
    runner: runner_info.ExtensionRunnerInfo,
) -> collections.abc.AsyncIterator[
    collections.abc.AsyncIterable[domain.PartialResultRawValue]
]:
    logger.trace(f"Run {action_name} in runner {runner.working_dir_path}")

    result: AsyncList[domain.PartialResultRawValue] = AsyncList()
    try:
        async with asyncio.TaskGroup() as tg:
            partial_results_task = tg.create_task(
                get_partial_results(
                    result_list=result, partial_result_token=partial_result_token
                )
            )
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
    file_path: Path,
    action_name: str,
    params: dict[str, Any],
    partial_result_token: int | str,
    ws_context: context.WorkspaceContext,
) -> collections.abc.AsyncIterator[runner_client.RunActionRawResult]:
    logger.trace(f"Run {action_name} on {file_path}")
    runner = find_action_project_runner(
        file_path=file_path, action_name=action_name, ws_context=ws_context
    )

    return run_with_partial_results(
        action_name=action_name,
        params=params,
        partial_result_token=partial_result_token,
        runner=runner,
    )


def find_all_projects_with_action(
    action_name: str, ws_context: context.WorkspaceContext
) -> list[Path]:
    projects = ws_context.ws_projects
    relevant_projects: dict[Path, domain.Project] = {
        path: project
        for path, project in projects.items()
        if project.status != domain.ProjectStatus.NO_FINECODE
    }

    # exclude not running projects and projects without requested action
    for project_dir_path, project_def in relevant_projects.copy().items():
        if project_def.status != domain.ProjectStatus.RUNNING:
            # projects that are not running, have no actions. Files of those projects
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

    relevant_projects_paths: list[Path] = list(relevant_projects.keys())
    return relevant_projects_paths


__all__ = [
    "find_action_project_and_run",
    "find_action_project_and_run_with_partial_results",
    "run_with_partial_results",
]
