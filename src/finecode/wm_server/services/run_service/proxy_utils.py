from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import typing

import ordered_set
from loguru import logger

from finecode import user_messages
from finecode.wm_server import find_project, context, domain, domain_helpers, wal
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.runner import runner_client
from finecode.wm_server.runner.runner_manager import RunnerFailedToStart
from finecode.wm_server.runner.runner_client import RunResultFormat  # reexport

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
            + f"status: {project_status.name}"
        )
        raise ActionRunFailed(
            f"Project {project_path} has no valid config with finecode,"
            + f"status: {project_status.name}"
        )

    return project_path


async def find_action_project_and_run(
    file_path: pathlib.Path,
    action_name: str,
    params: dict[str, typing.Any],
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
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
            run_trigger=run_trigger,
            dev_env=dev_env,
            initialize_all_handlers=initialize_all_handlers,
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
    except runner_client.BaseRunnerRequestException as exception:
        logger.error(f"Error on running action {action_name}: {exception.message}")
        raise ActionRunFailed(exception.message) from exception

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
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat] | None = None,
    progress_token: int | str | None = None,
) -> runner_client.RunActionResponse:
    options: dict[str, typing.Any] = {
        "partial_result_token": partial_result_token,
        "meta": {"trigger": run_trigger.value, "dev_env": dev_env.value},
    }
    if progress_token is not None:
        options["progress_token"] = progress_token
    if result_formats is not None:
        options["result_formats"] = result_formats
    logger.trace(f"run_action_and_notify: sending to runner {runner.readable_id}, action={action_name}, token={partial_result_token}, options_keys={list(options.keys())}")
    response = await run_action_in_runner(
        action_name=action_name,
        params=params,
        runner=runner,
        options=options,
    )
    logger.trace(f"run_action_and_notify: got response from runner {runner.readable_id}, return_code={response.return_code}, result_formats={list(response.result_by_format.keys())}")
    return response


async def get_partial_results(
    result_list: AsyncList,
    partial_result_token: int | str,
    runner: runner_client.ExtensionRunnerInfo,
) -> None:
    try:
        logger.trace(f"get_partial_results: listening on runner {runner.readable_id} for token={partial_result_token}")
        with runner.partial_results.iterator() as iterator:
            async for partial_result in iterator:
                logger.trace(f"get_partial_results: received partial from {runner.readable_id}, result_token={partial_result.token}, our_token={partial_result_token}, match={partial_result.token == partial_result_token}")
                if partial_result.token == partial_result_token:
                    value_preview = str(partial_result.value)[:200] if partial_result.value else "None"
                    logger.trace(f"get_partial_results: matched! value preview: {value_preview}")
                    result_list.append(partial_result.value)
    except asyncio.CancelledError:
        logger.trace(f"get_partial_results: cancelled for runner {runner.readable_id} token={partial_result_token}")


async def get_progress(
    result_list: AsyncList,
    progress_token: int | str,
    runner: runner_client.ExtensionRunnerInfo,
) -> None:
    try:
        logger.trace(f"get_progress: listening on runner {runner.readable_id} for token={progress_token}")
        with runner.progress_notifications.iterator() as iterator:
            async for notification in iterator:
                if notification.token == progress_token:
                    logger.trace(f"get_progress: matched type={notification.value.get('type')} from {runner.readable_id}")
                    result_list.append(notification.value)
    except asyncio.CancelledError:
        logger.trace(f"get_progress: cancelled for runner {runner.readable_id} token={progress_token}")


class RunWithPartialResultsContext:
    """Holds both the partial results async iterable and the final runner responses.

    ``partials`` is available immediately for iteration.  ``responses`` is
    populated after the context manager exits (i.e. after all runner tasks
    complete).  ``progress`` carries progress notifications (begin/report/end).
    """

    def __init__(
        self,
        partials: AsyncList[domain.PartialResultRawValue],
        progress: AsyncList[domain.ProgressRawValue] | None = None,
    ) -> None:
        self.partials = partials
        self.progress = progress
        self.responses: list[runner_client.RunActionResponse] = []

    def __aiter__(self):
        return self.partials.__aiter__()


@contextlib.asynccontextmanager
async def run_with_partial_results(
    action_name: str,
    params: dict[str, typing.Any],
    partial_result_token: int | str,
    project_dir_path: pathlib.Path,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
    result_formats: list[runner_client.RunResultFormat] | None = None,
    progress_token: int | str | None = None,
) -> collections.abc.AsyncIterator[RunWithPartialResultsContext]:
    logger.trace(f"Run {action_name} in project {project_dir_path}")

    result: AsyncList[domain.PartialResultRawValue] = AsyncList()
    progress_result: AsyncList[domain.ProgressRawValue] | None = None
    if progress_token is not None:
        progress_result = AsyncList()
    ctx = RunWithPartialResultsContext(partials=result, progress=progress_result)
    project = ws_context.ws_projects[project_dir_path]
    try:
        action_tasks: list[asyncio.Task] = []
        async with asyncio.TaskGroup() as tg:
            action = next(
                action for action in project.actions if action.name == action_name
            )
            action_envs = ordered_set.OrderedSet(
                [handler.env for handler in action.handlers]
            )
            for env_name in action_envs:
                try:
                    runner = await runner_manager.get_or_start_runner(
                        project_def=project,
                        env_name=env_name,
                        ws_context=ws_context,
                        initialize_all_handlers=initialize_all_handlers,
                        action_names_to_initialize=[action_name],
                    )
                except runner_manager.RunnerFailedToStart as exception:
                    raise ActionRunFailed(
                        f"Runner {env_name} in project {project.dir_path} failed: {exception.message}"
                    ) from exception

                runner_partial_results_task = tg.create_task(
                    get_partial_results(
                        result_list=result,
                        partial_result_token=partial_result_token,
                        runner=runner,
                    )
                )

                runner_progress_task: asyncio.Task | None = None
                if progress_token is not None and progress_result is not None:
                    runner_progress_task = tg.create_task(
                        get_progress(
                            result_list=progress_result,
                            progress_token=progress_token,
                            runner=runner,
                        )
                    )

                action_task = tg.create_task(
                    run_action_and_notify(
                        action_name=action_name,
                        params=params,
                        partial_result_token=partial_result_token,
                        runner=runner,
                        run_trigger=run_trigger,
                        dev_env=dev_env,
                        result_formats=result_formats,
                        progress_token=progress_token,
                    )
                )

                def _make_cleanup(
                    partial_task: asyncio.Task,
                    prog_task: asyncio.Task | None,
                ) -> typing.Callable[[asyncio.Future], None]:
                    def _cleanup(_fut: asyncio.Future) -> None:
                        logger.trace(f"run_action_and_notify: ending result_list, cancelling partial_results_task for token={partial_result_token}")
                        result.end()
                        partial_task.cancel("Got final result")
                        if progress_result is not None:
                            progress_result.end()
                        if prog_task is not None:
                            prog_task.cancel("Got final result")
                    return _cleanup

                action_task.add_done_callback(
                    _make_cleanup(runner_partial_results_task, runner_progress_task)
                )
                action_tasks.append(action_task)

            yield ctx
        # TaskGroup exited — all tasks completed, collect final responses
        for task in action_tasks:
            ctx.responses.append(task.result())
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
        ) from eg


@contextlib.asynccontextmanager
async def find_action_project_and_run_with_partial_results(
    file_path: pathlib.Path,
    action_name: str,
    params: dict[str, typing.Any],
    partial_result_token: int | str,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    ws_context: context.WorkspaceContext,
    initialize_all_handlers: bool = False,
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
        run_trigger=run_trigger,
        dev_env=dev_env,
        ws_context=ws_context,
        initialize_all_handlers=initialize_all_handlers,
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
        if not isinstance(project_def, domain.CollectedProject):
            # projects without collected actions cannot be matched
            continue

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
    initialize_handlers: bool = True,
    initialize_all_handlers: bool = False,
) -> None:
    """Collect all required envs from actions that will be run and start them.

    Args:
        initialize_handlers: Initialize handlers for the specified actions.
        initialize_all_handlers: Initialize all handlers in the environment,
            not just those for the specified actions. Takes precedence over
            initialize_handlers.
    """
    required_envs_by_project: dict[pathlib.Path, set[str]] = {}
    for project_dir_path, action_names in actions_by_projects.items():
        project = ws_context.ws_projects[project_dir_path]
        if isinstance(project, domain.CollectedProject):
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
                action_names = actions_by_projects[project_dir_path]

                for env_name in required_envs:
                    if initialize_all_handlers:
                        handlers_to_init = (
                            domain_helpers.collect_all_handlers_to_initialize(
                                project, env_name
                            )
                        )
                    elif initialize_handlers:
                        handlers_to_init = (
                            domain_helpers.collect_handlers_to_initialize_for_actions(
                                project, env_name, action_names
                            )
                        )
                    else:
                        handlers_to_init = None
                    tg.create_task(
                        _start_runner_or_update_config(
                            env_name=env_name,
                            existing_runners=existing_runners,
                            project=project,
                            update_config_in_running_runners=update_config_in_running_runners,
                            ws_context=ws_context,
                            handlers_to_initialize=handlers_to_init,
                        )
                    )
    except ExceptionGroup as eg:
        errors: list[str] = []
        for exception in eg.exceptions:
            if isinstance(exception, StartingEnvironmentsFailed):
                errors.append(exception.message)
            else:
                errors.append(str(exception))
        raise StartingEnvironmentsFailed(".".join(errors)) from eg


async def _start_runner_or_update_config(
    env_name: str,
    existing_runners: dict[str, runner_client.ExtensionRunnerInfo],
    project: domain.Project,
    update_config_in_running_runners: bool,
    ws_context: context.WorkspaceContext,
    handlers_to_initialize: dict[str, list[str]] | None,
):
    runner_exist = env_name in existing_runners
    start_runner = True
    if runner_exist:
        runner = existing_runners[env_name]
        if runner.status == runner_client.RunnerStatus.INITIALIZING:
            await runner.initialized_event.wait()

        runner_is_running = (
            runner.status == runner_client.RunnerStatus.RUNNING
        )
        start_runner = not runner_is_running

    if start_runner:
        try:
            await runner_manager.start_runner(
                project_def=project, env_name=env_name, handlers_to_initialize=handlers_to_initialize, ws_context=ws_context
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
                    runner=runner,
                    project=project,
                    handlers_to_initialize=handlers_to_initialize,
                    ws_context=ws_context
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
    result_formats: list[RunResultFormat],
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    progress_token_by_action: dict[str, str] | None = None,
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
                            run_trigger=run_trigger,
                            dev_env=dev_env,
                            result_formats=result_formats,
                            progress_token=progress_token_by_action.get(action_name) if progress_token_by_action else None,
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
            raise ActionRunFailed(f"Running of actions {actions} failed") from eg

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
                    run_trigger=run_trigger,
                    dev_env=dev_env,
                    result_formats=result_formats,
                    progress_token=progress_token_by_action.get(action_name) if progress_token_by_action else None,
                )
            except ActionRunFailed as exception:
                raise ActionRunFailed(
                    f"Running of action {action_name} failed: {exception.message}"
                ) from exception
            except Exception as error:
                logger.error("Unexpected exception")
                logger.exception(error)
                raise ActionRunFailed(
                    f"Running of action {action_name} failed with unexpected exception"
                ) from error

            result_by_action[action_name] = run_result

    return result_by_action


async def run_actions_in_projects(
    actions_by_project: dict[pathlib.Path, list[str]],
    action_payload: dict[str, str],
    ws_context: context.WorkspaceContext,
    concurrently: bool,
    result_formats: list[RunResultFormat],
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    payload_overrides_by_project: dict[str, dict[str, typing.Any]] | None = None,
    progress_token_by_project: dict[pathlib.Path, dict[str, str]] | None = None,
) -> dict[pathlib.Path, dict[str, RunActionResponse]]:
    _payload_overrides_by_project = payload_overrides_by_project or {}
    project_handler_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for project_dir_path, actions_to_run in actions_by_project.items():
                project = ws_context.ws_projects[project_dir_path]
                project_payload = {
                    **action_payload,
                    **_payload_overrides_by_project.get(str(project_dir_path), {}),
                }
                project_task = tg.create_task(
                    run_actions_in_running_project(
                        actions=actions_to_run,
                        action_payload=project_payload,
                        project=project,
                        ws_context=ws_context,
                        concurrently=concurrently,
                        result_formats=result_formats,
                        run_trigger=run_trigger,
                        dev_env=dev_env,
                        progress_token_by_action=progress_token_by_project.get(project_dir_path) if progress_token_by_project else None,
                    )
                )
                project_handler_tasks.append(project_task)
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            # TODO: merge all in one?
            raise exception from eg

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
        if not isinstance(project, domain.CollectedProject):
            continue
        project_actions_names = [action.name for action in project.actions]
        # find which of requested actions are available in the project
        action_to_run_in_project = actions_set & ordered_set.OrderedSet(
            project_actions_names
        )
        relevant_actions_in_project = list(action_to_run_in_project)
        if len(relevant_actions_in_project) > 0:
            actions_by_project[project.dir_path] = relevant_actions_in_project

    return actions_by_project


RunResultFormat: typing.TypeAlias = runner_client.RunResultFormat
RunActionResponse: typing.TypeAlias = runner_client.RunActionResponse
RunActionTrigger: typing.TypeAlias = runner_client.RunActionTrigger
DevEnv: typing.TypeAlias = runner_client.DevEnv


async def run_action(
    action_name: str,
    params: dict[str, typing.Any],
    project_def: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat] | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
) -> RunActionResponse:
    wal_run_id = wal.new_wal_run_id()
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action_name} with {formatted_params}")
    
    if result_formats is None:
        _result_formats = [RunResultFormat.JSON]
    else:
        _result_formats = result_formats

    if project_def.status != domain.ProjectStatus.CONFIG_VALID:
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_REJECTED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunRejectedPayload(reason="invalid_project_config"),
        )
        raise ActionRunFailed(
            f"Project {project_def.dir_path} has no valid configuration and finecode."
            + " Please check logs."
        )

    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUN_ACCEPTED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunAcceptedPayload(params_hash=wal.params_hash(params)),
    )

    payload = params

    # cases:
    # - base: all action handlers are in one env
    #   -> send `run_action` request to runner in env and let it handle concurrency etc.
    #      It could be done also in workspace manager, but handlers share run context
    # - mixed envs: action handlers are in different envs
    # -- concurrent execution of handlers
    # -- sequential execution of handlers
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
            run_trigger=run_trigger,
            dev_env=dev_env,
            result_formats=_result_formats,
            initialize_all_handlers=initialize_all_handlers,
            progress_token=progress_token,
            wal_run_id=wal_run_id,
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
                    run_trigger=run_trigger,
                    dev_env=dev_env,
                    result_formats=_result_formats,
                    initialize_all_handlers=initialize_all_handlers,
                    progress_token=progress_token,
                    wal_run_id=wal_run_id,
                )

    return response


async def _run_action_in_env_runner(
    action_name: str,
    payload: dict[str, typing.Any],
    env_name: str,
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat],
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    wal_run_id: str | None = None,
):
    effective_wal_run_id = wal_run_id or wal.new_wal_run_id()
    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUNNER_SELECTED,
        wal_run_id=effective_wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunnerSelectedPayload(env_name=env_name),
    )

    try:
        runner = await runner_manager.get_or_start_runner(
            project_def=project_def,
            env_name=env_name,
            ws_context=ws_context,
            initialize_all_handlers=initialize_all_handlers,
            action_names_to_initialize=[action_name],
        )
    except runner_manager.RunnerFailedToStart as exception:
        raise ActionRunFailed(
            f"Runner {env_name} in project {project_def.dir_path} failed: {exception.message}"
        ) from exception

    try:
        options: dict[str, typing.Any] = {
            "result_formats": result_formats,
            "meta": {"trigger": run_trigger.value, "dev_env": dev_env.value},
        }
        if progress_token is not None:
            options["progress_token"] = progress_token
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_DISPATCHED,
            wal_run_id=effective_wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunDispatchedPayload(
                runner_id=runner.readable_id,
                env_name=env_name,
            ),
        )
        response = await runner_client.run_action(
            runner=runner,
            action_name=action_name,
            params=payload,
            options=options,
        )
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_COMPLETED,
            wal_run_id=effective_wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunCompletedPayload(return_code=response.return_code),
        )
    except runner_client.BaseRunnerRequestException as error:
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_FAILED,
            wal_run_id=effective_wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunFailedPayload(error=error.message, env_name=env_name),
        )
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
