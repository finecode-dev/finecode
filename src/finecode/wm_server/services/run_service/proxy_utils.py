from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import pathlib
import typing

import ordered_set
from loguru import logger

from finecode import telemetry, user_messages
from finecode.wm_server import find_project, context, domain, domain_helpers, wal
from finecode.wm_server.runner import runner_manager
from finecode.wm_server.runner import runner_client
from finecode.wm_server.runner.runner_manager import RunnerFailedToStart
from finecode.wm_server.services import runner_start_service
from finecode.wm_server.runner.runner_client import RunResultFormat  # reexport

from .exceptions import ActionCancelledError, ActionRunFailed, StartingEnvironmentsFailed


def _format_runner_failure_message(
    action_name: str,
    runner: runner_client.ExtensionRunnerInfo,
    base_error_message: str,
) -> str:
    """Build a stable WM error message for runner failures.

    The WM owns runner log paths, so this is the single place where we append
    a "Log file:" hint. The helper is idempotent to avoid duplicated suffixes
    when nested orchestration re-wraps propagated error messages.
    """
    error_message = base_error_message
    if "Log file:" not in error_message:
        error_message = f"{error_message} . Log file: {runner.logs_path}"

    action_prefix = f"Action {action_name} failed in {runner.readable_id}:"
    if error_message.startswith(action_prefix):
        return error_message
    return f"{action_prefix} {error_message}"


async def find_action_project(
    file_path: pathlib.Path, action_name: str, ws_context: context.WorkspaceContext
) -> pathlib.Path:
    try:
        project_path = await find_project.find_project_with_action_for_file(
            file_path=file_path,
            action_name=action_name,
            ws_context=ws_context,
        )
    except find_project.FileNotInWorkspaceError:
        raise
    except find_project.FileHasNoActionError:
        raise
    except ValueError as error:
        logger.warning(f"Skip {action_name} on {file_path}: {error}")
        raise ActionRunFailed(str(error)) from error

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

    response = await run_action(
        action_name=action_name,
        params=params,
        project_def=project,
        ws_context=ws_context,
        run_trigger=run_trigger,
        dev_env=dev_env,
        initialize_all_handlers=initialize_all_handlers,
    )
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
        raise ActionRunFailed(
            _format_runner_failure_message(
                action_name=action_name,
                runner=runner,
                base_error_message=exception.message,
            )
        ) from exception

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
            # Check data BEFORE checking ended: items may have been appended
            # just before end() was called but after we started waiting. If we
            # checked ended first we would silently drop those items.
            if len(self.async_list.data) <= self.current_index:
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
    wal_run_id: str,
    result_formats: list[runner_client.RunResultFormat] | None = None,
    progress_token: int | str | None = None,
    caller_kwargs: dict | None = None,
) -> runner_client.RunActionResponse:
    options: dict[str, typing.Any] = {
        "partialResultToken": partial_result_token,
        "walRunId": wal_run_id,
        "meta": {"trigger": run_trigger.value, "devEnv": dev_env.value},
    }
    if progress_token is not None:
        options["progressToken"] = progress_token
    if result_formats is not None:
        options["resultFormats"] = result_formats
    if caller_kwargs is not None:
        options["callerKwargs"] = caller_kwargs
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
    caller_kwargs: dict | None = None,
) -> collections.abc.AsyncIterator[RunWithPartialResultsContext]:
    logger.trace(f"Run {action_name} in project {project_dir_path}")
    wal_run_id = wal.new_wal_run_id()

    with telemetry.action_run_span(action_name, project_dir_path, wal_run_id, dev_env=dev_env.value):
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
                if not action_envs:
                    await user_messages.warning(
                        f"Action '{action_name}' has no handlers configured in project"
                        f" '{project.dir_path}'. No tasks will run."
                    )
                    result.end()
                    if progress_result is not None:
                        progress_result.end()
                for env_name in action_envs:
                    try:
                        runner = await runner_start_service.get_or_start_runner_with_auto_prepare(
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
                            wal_run_id=wal_run_id,
                            result_formats=result_formats,
                            progress_token=progress_token,
                            caller_kwargs=caller_kwargs,
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


def find_all_projects_with_action(
    action_name: str, ws_context: context.WorkspaceContext
) -> list[pathlib.Path]:
    projects = ws_context.ws_projects
    relevant_projects: dict[pathlib.Path, domain.Project] = {
        path: project
        for path, project in projects.items()
        if project.status != domain.ProjectStatus.NO_FINECODE
    }

    # exclude projects that are not fully resolved and projects without requested action
    for project_dir_path, project_def in relevant_projects.copy().items():
        if not isinstance(project_def, domain.ResolvedProject):
            # unresolved projects (CollectedProject or plain Project) have incomplete
            # action sets — preset-contributed actions are not yet visible
            del relevant_projects[project_dir_path]
            continue

        try:
            next(action for action in project_def.actions if action.name == action_name)
        except StopIteration:
            del relevant_projects[project_dir_path]
            continue

    relevant_projects_paths: list[pathlib.Path] = list(relevant_projects.keys())
    return relevant_projects_paths


async def ensure_action_metadata(
    action: domain.Action,
    project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> None:
    """Ensure action metadata (scope, canonical_source, etc.) is resolved.

    The unified signal for resolved metadata is ``canonical_source is not None``:
    ``update_runner_config`` sets canonical_source, scope, runs_concurrently,
    parent_action_source, and language all in one pass when the ER imports the
    action class.  Until then all of those fields are ``None`` / default and
    cannot be trusted for dispatch decisions.

    When metadata is absent, starts the first handler env runner (the env whose
    package contains the action class), which causes ``update_runner_config`` to
    resolve and propagate the metadata to all other projects.

    Raises ``ActionNotResolvableError`` if the environment cannot be started or
    the action class cannot be imported in that environment.
    """
    if action.canonical_source is not None:
        return

    if not action.handlers:
        from finecode.wm_server.errors import ActionNotResolvableError
        raise ActionNotResolvableError(
            f"Action '{action.source}' has no handlers configured — "
            f"its metadata cannot be resolved from any ER."
        )

    resolution_env = action.handlers[0].env
    existing_runners = ws_context.ws_projects_extension_runners.get(project.dir_path, {})
    try:
        await _start_runner_or_update_config(
            env_name=resolution_env,
            existing_runners=existing_runners,
            project=project,
            ws_context=ws_context,
            handlers_to_initialize=None,
        )
    except StartingEnvironmentsFailed as exc:
        from finecode.wm_server.errors import ActionNotResolvableError
        raise ActionNotResolvableError(
            f"Action '{action.source}' metadata could not be resolved: "
            f"failed to start env '{resolution_env}' in project '{project.name}'. "
            f"Ensure the environment is prepared (finecode prepare-envs). "
            f"Details: {exc.message}"
        ) from exc

    if action.canonical_source is None:
        from finecode.wm_server.errors import ActionNotResolvableError
        raise ActionNotResolvableError(
            f"Action '{action.source}' metadata was not resolved after starting "
            f"env '{resolution_env}' in project '{project.name}'. "
            f"Ensure the action class is importable in that environment."
        )


async def find_subactions_for_parent(
    parent_action_source: str,
    project: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
) -> list[domain.Action]:
    """Return every action in *project* that specializes *parent_action_source*.

    Per ADR-0045, this is the WM-owned answer to "what subactions exist for
    this parent", regardless of which env each one's handler runs in — an ER
    only ever knows its own env's actions, so it cannot answer this on its
    own. An action's specialization relationship (``parent_action_source`` /
    ``language``) is only known once its class has been imported by some ER,
    so unresolved actions are resolved on demand via
    :func:`ensure_action_metadata` (best-effort — an action that cannot be
    resolved at all is simply not a candidate, rather than failing the whole
    query).
    """
    result: list[domain.Action] = []
    for action in project.actions:
        if action.canonical_source is None:
            try:
                await ensure_action_metadata(action, project, ws_context)
            except Exception as exc:
                logger.debug(
                    f"Could not resolve metadata for '{action.source}' while"
                    f" looking for subactions of '{parent_action_source}': {exc}"
                )
                continue
        if (
            action.parent_action_source == parent_action_source
            and action.language is not None
        ):
            result.append(action)
    return result


async def start_required_environments(
    actions_by_projects: dict[pathlib.Path, list[str]],
    ws_context: context.WorkspaceContext,
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
    ws_context: context.WorkspaceContext,
    handlers_to_initialize: dict[str, list[str]] | None,
):
    runner_exist = env_name in existing_runners
    start_runner = True
    if runner_exist:
        runner = existing_runners[env_name]
        if runner.status == runner_client.RunnerStatus.INITIALIZING:
            await runner.initialized_event.wait()
        elif runner.status == runner_client.RunnerStatus.REPAIRING:
            if runner.repair_complete_event is not None:
                await runner.repair_complete_event.wait()
            runner = existing_runners.get(env_name, runner)

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
            failed_runner = ws_context.ws_projects_extension_runners.get(
                project.dir_path, {}
            ).get(env_name)
            if (
                failed_runner is None
                or failed_runner.status != runner_client.RunnerStatus.NO_VENV
            ):
                raise StartingEnvironmentsFailed(
                    f"Failed to start runner for env '{env_name}' in project '{project.name}': {exception.message}"
                ) from exception

            # Venv is missing — either it never existed, or get_python_cmd just wiped
            # it after detecting a stale (relocated) venv. Either way, auto-repair the
            # same way get_or_start_runner_with_auto_prepare does, instead of surfacing
            # a bare startup failure that requires a manual `prepare-envs` run.
            from finecode.wm_server.services.prepare_envs_service import (
                PrepareEnvsFailed,
            )

            try:
                await runner_start_service.repair_no_venv_env(project, env_name, ws_context)
            except PrepareEnvsFailed as prep_exc:
                raise StartingEnvironmentsFailed(
                    f"Failed to start runner for env '{env_name}' in project '{project.name}': {prep_exc.message}"
                ) from prep_exc
            except runner_manager.RunnerFailedToStart as restart_exc:
                raise StartingEnvironmentsFailed(
                    f"Auto prepare-envs succeeded but runner restart failed for env "
                    f"'{env_name}' in project '{project.name}': {restart_exc.message}"
                ) from restart_exc


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
            error_messages: list[str] = []
            for exception in eg.exceptions:
                if isinstance(exception, ActionRunFailed):
                    logger.error(f"{exception.message} in {project.name}")
                    error_messages.append(exception.message)
                else:
                    logger.error("Unexpected exception:")
                    logger.exception(exception)
                    error_messages.append(str(exception))
            combined = "; ".join(error_messages)
            raise ActionRunFailed(
                f"Running of actions {actions} in project '{project.dir_path}' failed: {combined}"
            ) from eg

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
                # Keep original context to avoid repetitive nested wrappers.
                raise exception
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

    # Lazily start runners for projects that are not yet resolved.  This handles
    # the case where a workspace-scope action fans out to projects whose runners
    # were not started upfront (e.g. CLI run with --project filter).
    unresolved = [
        ws_context.ws_projects[p]
        for p in actions_by_project
        if not isinstance(ws_context.ws_projects.get(p), domain.ResolvedProject)
        and ws_context.ws_projects.get(p) is not None
    ]
    if unresolved:
        from finecode.wm_server.services import runner_start_service
        unresolved_names = ", ".join(p.name for p in unresolved)
        logger.debug(
            f"Lazily starting runners for {len(unresolved)} unresolved project(s): {unresolved_names}"
        )
        await runner_start_service.start_runners_with_auto_prepare(
            projects=unresolved,
            ws_context=ws_context,
            initialize_all_handlers=True,
        )

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
        error_messages = []
        for exception in eg.exceptions:
            if isinstance(exception, ActionRunFailed):
                error_messages.append(exception.message)
            else:
                logger.exception(exception)
                error_messages.append(str(exception))
        raise ActionRunFailed("; ".join(error_messages)) from eg

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
        if not isinstance(project, domain.ResolvedProject):
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
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat] | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    orchestration_depth: int = 0,
    caller_kwargs: dict | None = None,
    allow_no_handlers: bool = False,
) -> RunActionResponse:
    """Run a single action in the project's extension runner(s).

    ``project_def`` must be a :class:`~finecode.wm_server.domain.ResolvedProject`
    at call time.  A plain ``Project`` or ``CollectedProject`` (presets not yet
    resolved) is rejected with a WAL ``RUN_REJECTED`` event and an
    :exc:`ActionRunFailed` exception.  Callers that retrieve a project from
    ``ws_context.ws_projects`` may receive any subtype — validation happens here
    so call sites do not need to duplicate the check.
    """
    wal_run_id = wal.new_wal_run_id()
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action_name} with {formatted_params}")

    if result_formats is None:
        _result_formats = [RunResultFormat.JSON]
    else:
        _result_formats = result_formats

    with telemetry.action_metrics(action_name, project_def.dir_path.name), telemetry.action_run_span(action_name, project_def.dir_path, wal_run_id, dev_env=dev_env.value, orchestration_depth=orchestration_depth):
        if not isinstance(project_def, domain.ResolvedProject):
            wal.emit_run_event(
                ws_context.wal_writer,
                event_type=wal.WalEventType.RUN_REJECTED,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=project_def.dir_path,
                run_trigger=run_trigger.value,
                dev_env=dev_env.value,
                payload=wal.RunRejectedPayload(reason="project_not_resolved"),
            )
            telemetry.add_span_event("run.rejected", {"reason": "project_not_resolved"})
            runner = (
                ws_context.ws_projects_extension_runners
                .get(project_def.dir_path, {})
                .get("dev_workspace")
            )
            if runner is not None:
                runner_detail = f"runner status={runner.status.name}"
                if runner.log_file_path is not None:
                    runner_detail += f", logs={runner.log_file_path}"
            else:
                runner_detail = "no runner started"
            raise ActionRunFailed(
                f"Project {project_def.dir_path} is not ready to run actions yet"
                f" ({runner_detail})."
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
        telemetry.add_span_event("run.accepted")

        payload = params
        # Captured here (inside action_run_span, outside er_dispatch_span) so that
        # handler_span on the ER becomes a child of action_run_span.  This explicit
        # application-level propagation is required for multi-hop chains
        # (WM → ER1 → WM → ER2 → …): JsonRpcServerSession is long-running and holds
        # no per-request OTel context, so ambient context cannot carry the parent
        # across process boundaries.  See ITracingHooks docstring for the full rationale.
        traceparent = telemetry.get_current_traceparent()

        # cases:
        # - base: all action handlers are in one env
        #   -> send `run_action` request to runner in env and let it handle concurrency etc.
        #      It could be done also in workspace manager, but handlers share run context
        # - mixed envs: action handlers are in different envs
        # -- concurrent execution of handlers
        # -- sequential execution of handlers
        try:
            action = next(
                action for action in project_def.actions if action.name == action_name
            )
        except StopIteration:
            raise ActionRunFailed(
                f"Action '{action_name}' not found in project '{project_def.dir_path}'"
            )
        all_handlers_envs = ordered_set.OrderedSet(
            [handler.env for handler in action.handlers]
        )
        all_handlers_are_in_one_env = len(all_handlers_envs) == 1

        if not all_handlers_envs:
            if allow_no_handlers:
                logger.info(
                    f"Action '{action_name}' has no handlers (expected by caller)"
                )
            else:
                logger.warning(
                    f"Action '{action_name}' has no handlers — check your configuration"
                )
            return RunActionResponse(
                result_by_format={},
                return_code=0,
                status="no_handlers",
            )

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
                traceparent=traceparent,
                orchestration_depth=orchestration_depth,
                caller_kwargs=caller_kwargs,
            )
        else:
            run_concurrently = action.runs_concurrently

            if run_concurrently:
                response = await _run_multi_env_concurrent(
                    action_name=action_name,
                    action=action,
                    payload=payload,
                    project_def=project_def,
                    ws_context=ws_context,
                    run_trigger=run_trigger,
                    dev_env=dev_env,
                    result_formats=_result_formats,
                    initialize_all_handlers=initialize_all_handlers,
                    progress_token=progress_token,
                    wal_run_id=wal_run_id,
                    traceparent=traceparent,
                    orchestration_depth=orchestration_depth,
                    caller_kwargs=caller_kwargs,
                )
            else:
                response = await _run_multi_env_sequential(
                    action_name=action_name,
                    action=action,
                    payload=payload,
                    project_def=project_def,
                    ws_context=ws_context,
                    run_trigger=run_trigger,
                    dev_env=dev_env,
                    result_formats=_result_formats,
                    initialize_all_handlers=initialize_all_handlers,
                    progress_token=progress_token,
                    wal_run_id=wal_run_id,
                    traceparent=traceparent,
                    orchestration_depth=orchestration_depth,
                    caller_kwargs=caller_kwargs,
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
    wal_run_id: str,
    traceparent: str | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    orchestration_depth: int = 0,
    caller_kwargs: dict | None = None,
):
    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUNNER_SELECTED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunnerSelectedPayload(env_name=env_name),
    )
    telemetry.add_span_event("runner.selected", {"env_name": env_name})

    try:
        with telemetry.runner_start_span(env_name):
            runner = await runner_start_service.get_or_start_runner_with_auto_prepare(
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
            "resultFormats": result_formats,
            "walRunId": wal_run_id,
            "traceparent": traceparent,
            "meta": {"trigger": run_trigger.value, "devEnv": dev_env.value, "orchestrationDepth": orchestration_depth},
        }
        if progress_token is not None:
            options["progressToken"] = progress_token
        if caller_kwargs is not None:
            options["callerKwargs"] = caller_kwargs
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_DISPATCHED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunDispatchedPayload(
                runner_id=runner.readable_id,
                env_name=env_name,
            ),
        )
        telemetry.add_span_event("run.dispatched", {"env_name": env_name, "runner_id": runner.readable_id})
        with telemetry.er_dispatch_span(env_name, runner.readable_id, action_name):
            response = await runner_client.run_action(
                runner=runner,
                action_name=action_name,
                params=payload,
                options=options,
            )
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_COMPLETED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunCompletedPayload(return_code=response.return_code),
        )
        telemetry.add_span_event("run.completed", {"return_code": response.return_code})
    except runner_client.BaseRunnerRequestException as error:
        if isinstance(error, runner_client.ActionRunCancelled):
            wal.emit_run_event(
                ws_context.wal_writer,
                event_type=wal.WalEventType.RUN_FAILED,
                wal_run_id=wal_run_id,
                action_name=action_name,
                project_path=project_def.dir_path,
                run_trigger=run_trigger.value,
                dev_env=dev_env.value,
                payload=wal.RunFailedPayload(
                    error=f"cancelled: {error.message}", env_name=env_name
                ),
            )
            telemetry.add_span_event(
                "run.cancelled", {"env_name": env_name, "error": error.message}
            )
            logger.debug(
                f"Action {action_name} cancelled in {env_name}: {error.message}"
            )
            raise ActionCancelledError(error.message) from error

        error_message = _format_runner_failure_message(
            action_name=action_name,
            runner=runner,
            base_error_message=error.message,
        )
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_FAILED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunFailedPayload(error=error_message, env_name=env_name),
        )
        telemetry.add_span_event("run.failed", {"env_name": env_name, "error": error_message})
        await user_messages.error(error_message)
        raise ActionRunFailed(error_message) from error

    return response



def _build_sequential_segments(
    handlers: list[domain.ActionHandler],
) -> list[tuple[str, list[str]]]:
    """Group consecutive same-env handlers into ordered segments.

    Example::
        [h1/env1, h2/env1, h3/env2, h4/env1]
        → [(env1, [h1, h2]), (env2, [h3]), (env1, [h4])]
    """
    segments: list[tuple[str, list[str]]] = []
    for handler in handlers:
        if segments and segments[-1][0] == handler.env:
            segments[-1][1].append(handler.name)
        else:
            segments.append((handler.env, [handler.name]))
    return segments


def _build_concurrent_groups(
    handlers: list[domain.ActionHandler],
) -> dict[str, list[str]]:
    """Group handlers by env for concurrent dispatch (order within env preserved).

    Example::
        [h1/env1, h2/env2, h3/env1]
        → {env1: [h1, h3], env2: [h2]}
    """
    groups: dict[str, list[str]] = {}
    for handler in handlers:
        groups.setdefault(handler.env, []).append(handler.name)
    return groups


async def _run_handlers_in_env_runner(
    action_name: str,
    handler_names: list[str],
    payload: dict[str, typing.Any],
    previous_result: dict | None,
    env_name: str,
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat],
    wal_run_id: str,
    traceparent: str | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    orchestration_depth: int = 0,
    previous_context: dict | None = None,
    caller_kwargs: dict | None = None,
) -> runner_client.RunHandlersResponse:
    """Call actions/runHandlers on the ER for one segment of a multi-env run."""
    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUNNER_SELECTED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunnerSelectedPayload(env_name=env_name),
    )
    telemetry.add_span_event("runner.selected", {"env_name": env_name})

    try:
        runner = await runner_start_service.get_or_start_runner_with_auto_prepare(
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

    options: dict[str, typing.Any] = {
        "resultFormats": result_formats,
        "walRunId": wal_run_id,
        "traceparent": traceparent,
        "meta": {
            "trigger": run_trigger.value,
            "devEnv": dev_env.value,
            "orchestrationDepth": orchestration_depth,
        },
    }
    if progress_token is not None:
        options["progressToken"] = progress_token

    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUN_DISPATCHED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunDispatchedPayload(
            runner_id=runner.readable_id,
            env_name=env_name,
        ),
    )
    telemetry.add_span_event("run.dispatched", {"env_name": env_name, "runner_id": runner.readable_id})

    try:
        response = await runner_client.run_handlers(
            runner=runner,
            action_name=action_name,
            handler_names=handler_names,
            params=payload,
            previous_result=previous_result,
            previous_context=previous_context,
            caller_kwargs=caller_kwargs,
            options=options,
        )
    except runner_client.BaseRunnerRequestException as error:
        error_message = _format_runner_failure_message(
            action_name=action_name,
            runner=runner,
            base_error_message=error.message,
        )
        wal.emit_run_event(
            ws_context.wal_writer,
            event_type=wal.WalEventType.RUN_FAILED,
            wal_run_id=wal_run_id,
            action_name=action_name,
            project_path=project_def.dir_path,
            run_trigger=run_trigger.value,
            dev_env=dev_env.value,
            payload=wal.RunFailedPayload(error=error_message, env_name=env_name),
        )
        telemetry.add_span_event("run.failed", {"env_name": env_name, "error": error_message})
        await user_messages.error(error_message)
        raise ActionRunFailed(error_message) from error

    wal.emit_run_event(
        ws_context.wal_writer,
        event_type=wal.WalEventType.RUN_COMPLETED,
        wal_run_id=wal_run_id,
        action_name=action_name,
        project_path=project_def.dir_path,
        run_trigger=run_trigger.value,
        dev_env=dev_env.value,
        payload=wal.RunCompletedPayload(return_code=response.return_code),
    )
    telemetry.add_span_event("run.completed", {"return_code": response.return_code})
    return response


async def _run_multi_env_sequential(
    action_name: str,
    action: domain.Action,
    payload: dict[str, typing.Any],
    project_def: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat],
    wal_run_id: str,
    traceparent: str | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    orchestration_depth: int = 0,
    caller_kwargs: dict | None = None,
) -> runner_client.RunActionResponse:
    """Drive multi-env sequential execution segment-by-segment."""
    segments = _build_sequential_segments(action.handlers)
    previous_result: dict | None = None
    previous_context: dict | None = None
    final_response: runner_client.RunHandlersResponse | None = None

    for idx, (env_name, handler_names) in enumerate(segments):
        is_last = idx == len(segments) - 1
        segment_formats = result_formats if is_last else []

        seg_response = await _run_handlers_in_env_runner(
            action_name=action_name,
            handler_names=handler_names,
            payload=payload,
            previous_result=previous_result,
            previous_context=previous_context,
            env_name=env_name,
            project_def=project_def,
            ws_context=ws_context,
            run_trigger=run_trigger,
            dev_env=dev_env,
            result_formats=segment_formats,
            wal_run_id=wal_run_id,
            traceparent=traceparent,
            initialize_all_handlers=initialize_all_handlers,
            progress_token=progress_token if is_last else None,
            orchestration_depth=orchestration_depth,
            caller_kwargs=caller_kwargs,
        )

        if seg_response.status == "stopped":
            return runner_client.RunActionResponse(
                result_by_format=seg_response.result_by_format,
                return_code=seg_response.return_code,
                status="stopped",
            )

        previous_result = seg_response.raw_result
        previous_context = seg_response.context
        final_response = seg_response

    assert final_response is not None
    return runner_client.RunActionResponse(
        result_by_format=final_response.result_by_format,
        return_code=final_response.return_code,
        status=final_response.status,
    )


async def _run_multi_env_concurrent(
    action_name: str,
    action: domain.Action,
    payload: dict[str, typing.Any],
    project_def: domain.CollectedProject,
    ws_context: context.WorkspaceContext,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat],
    wal_run_id: str,
    traceparent: str | None = None,
    initialize_all_handlers: bool = False,
    progress_token: int | str | None = None,
    orchestration_depth: int = 0,
    caller_kwargs: dict | None = None,
) -> runner_client.RunActionResponse:
    """Drive multi-env concurrent execution: dispatch all env groups in parallel."""
    groups = _build_concurrent_groups(action.handlers)
    group_tasks: list[asyncio.Task] = []

    try:
        async with asyncio.TaskGroup() as tg:
            for env_name, handler_names in groups.items():
                task = tg.create_task(
                    _run_handlers_in_env_runner(
                        action_name=action_name,
                        handler_names=handler_names,
                        payload=payload,
                        previous_result=None,
                        env_name=env_name,
                        project_def=project_def,
                        ws_context=ws_context,
                        run_trigger=run_trigger,
                        dev_env=dev_env,
                        result_formats=[],
                        wal_run_id=wal_run_id,
                        traceparent=traceparent,
                        initialize_all_handlers=initialize_all_handlers,
                        progress_token=None,
                        orchestration_depth=orchestration_depth,
                        caller_kwargs=caller_kwargs,
                    )
                )
                group_tasks.append(task)
    except ExceptionGroup as eg:
        errors = [
            exc.message if isinstance(exc, ActionRunFailed) else str(exc)
            for exc in eg.exceptions
        ]
        raise ActionRunFailed(
            f"Concurrent multi-env run of {action_name} failed: {', '.join(errors)}"
        ) from eg

    raw_results = [task.result().raw_result for task in group_tasks]

    if len(raw_results) == 1:
        merged_raw = raw_results[0]
    else:
        # Pick any running runner to call merge_results
        runners_by_env = ws_context.ws_projects_extension_runners.get(project_def.dir_path, {})
        merge_runner: runner_client.ExtensionRunnerInfo | None = None
        for env_name in groups:
            candidate = runners_by_env.get(env_name)
            if candidate is not None and candidate.status == runner_client.RunnerStatus.RUNNING:
                merge_runner = candidate
                break

        if merge_runner is None:
            if not groups:
                raise ActionRunFailed(
                    f"Cannot merge results for {action_name}: no env groups were built "
                    f"(action has no handlers assigned to any environment)"
                )
            non_running = {
                env: runners_by_env[env].status.value
                for env in groups
                if env in runners_by_env
            }
            missing = [env for env in groups if env not in runners_by_env]
            details = ", ".join(
                [f"{env}={status}" for env, status in non_running.items()]
                + [f"{env}=<not found>" for env in missing]
            )
            raise ActionRunFailed(
                f"Cannot merge results for {action_name}: no runner is in RUNNING status "
                f"among the env groups {list(groups.keys())}. "
                f"Runner statuses: {details}"
            )

        try:
            merged_raw = await runner_client.merge_results(
                runner=merge_runner,
                action_name=action_name,
                results=raw_results,
            )
        except runner_client.BaseRunnerRequestException as error:
            raise ActionRunFailed(
                f"merge_results failed for {action_name}: {error.message}"
            ) from error

    # Now format the merged result via a final run_handlers call on any env
    # with an empty handler list but the merged previousResult, requesting the
    # desired formats.  Use the last env in the group as a convenient runner.
    last_env = next(reversed(groups))
    format_response = await _run_handlers_in_env_runner(
        action_name=action_name,
        handler_names=[],
        payload=payload,
        previous_result=merged_raw,
        env_name=last_env,
        project_def=project_def,
        ws_context=ws_context,
        run_trigger=run_trigger,
        dev_env=dev_env,
        result_formats=result_formats,
        wal_run_id=wal_run_id,
        traceparent=traceparent,
        initialize_all_handlers=initialize_all_handlers,
        progress_token=progress_token,
        orchestration_depth=orchestration_depth,
        caller_kwargs=caller_kwargs,
    )

    return runner_client.RunActionResponse(
        result_by_format=format_response.result_by_format,
        return_code=format_response.return_code,
        status=format_response.status,
    )


__all__ = [
    "find_action_project_and_run",
    "find_projects_with_actions",
    "find_all_projects_with_action",
    "run_with_partial_results",
    "start_required_environments",
]
