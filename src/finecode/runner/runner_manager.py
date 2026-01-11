"""
API to manage ERs: start, stop, restart.
"""

import asyncio
import collections.abc
import concurrent.futures
import json
import os
import shutil
from pathlib import Path
import typing

from loguru import logger

from finecode import context, domain, finecode_cmd
from finecode.config import collect_actions, config_models, read_configs
from finecode.runner import (
    runner_client,
    _internal_client_api,
    _internal_client_types,
)
import finecode_jsonrpc as jsonrpc_client
from finecode_jsonrpc import _io_thread
from finecode.utils import iterable_subscribe

project_changed_callback: (
    typing.Callable[[domain.Project], collections.abc.Coroutine[None, None, None]]
    | None
) = None
# get_document: typing.Callable[[], collections.abc.Coroutine] | None = None
apply_workspace_edit: typing.Callable[[], collections.abc.Coroutine] | None = None
start_debug_session: typing.Callable[[int], collections.abc.Coroutine] | None = None
partial_results: iterable_subscribe.IterableSubscribe = (
    iterable_subscribe.IterableSubscribe()
)

# reexport
RunnerFailedToStart = jsonrpc_client.RunnerFailedToStart


async def notify_project_changed(project: domain.Project) -> None:
    if project_changed_callback is not None:
        await project_changed_callback(project)


async def _apply_workspace_edit(
    params: _internal_client_types.ApplyWorkspaceEditParams,
):
    def map_change_object(change):
        return _internal_client_types.TextEdit(
            range=_internal_client_types.Range(
                start=_internal_client_types.Position(
                    line=change.range.start.line, character=change.range.start.character
                ),
                end=_internal_client_types.Position(
                    change.range.end.line, character=change.range.end.character
                ),
            ),
            new_text=change.newText,
        )

    converted_params = _internal_client_types.ApplyWorkspaceEditParams(
        edit=_internal_client_types.WorkspaceEdit(
            document_changes=[
                _internal_client_types.TextDocumentEdit(
                    text_document=_internal_client_types.OptionalVersionedTextDocumentIdentifier(
                        document_edit.text_document.uri
                    ),
                    edits=[map_change_object(change) for change in document_edit.edits],
                )
                for document_edit in params.edit.document_changes
                if isinstance(document_edit, _internal_client_types.TextDocumentEdit)
            ]
        )
    )
    return await apply_workspace_edit(converted_params)


async def _start_extension_runner_process(
    runner: runner_client.ExtensionRunnerInfo, ws_context: context.WorkspaceContext, debug: bool = False
) -> None:
    try:
        python_cmd = finecode_cmd.get_python_cmd(
            runner.working_dir_path, runner.env_name
        )
    except ValueError as exception:
        try:
            runner.status = runner_client.RunnerStatus.NO_VENV
            await notify_project_changed(
                ws_context.ws_projects[runner.working_dir_path]
            )
        except KeyError:
            ...
        logger.error(
            f"Project {runner.working_dir_path} uses finecode, but env (venv) doesn't exist yet. Run `prepare_env` command to create it"
        )

        raise jsonrpc_client.RunnerFailedToStart(
            f"Runner '{runner.readable_id}' failed to start"
        ) from exception

    if ws_context.runner_io_thread is None:
        logger.trace("Starting IO Thread")
        ws_context.runner_io_thread = _io_thread.AsyncIOThread()
        ws_context.runner_io_thread.start()

    process_args: list[str] = [
        "--trace",
        f"--project-path={runner.working_dir_path.as_posix()}",
        f"--env-name={runner.env_name}",
    ]
    env_config = ws_context.ws_projects[runner.working_dir_path].env_configs[
        runner.env_name
    ]
    runner_config = env_config.runner_config

    start_with_debug = debug or runner_config.debug
    if start_with_debug:
        process_args.append("--debug")
        debug_port_future = concurrent.futures.Future()
    else:
        debug_port_future = None

    process_args_str: str = " ".join(process_args)
    client = jsonrpc_client.JsonRpcClient(message_types=_internal_client_types.METHOD_TO_TYPES, readable_id=runner.readable_id)
    
    try:
        await client.start(server_cmd=f"{python_cmd} -m finecode_extension_runner.cli start {process_args_str}", working_dir_path=runner.working_dir_path, io_thread=ws_context.runner_io_thread, debug_port_future=debug_port_future, connect=not start_with_debug)
    except jsonrpc_client.RunnerFailedToStart as exception:
        logger.error(f"Runner {runner.readable_id} failed to start: {exception.message}")
        runner.status = runner_client.RunnerStatus.FAILED
        runner.initialized_event.set()
        raise exception

    runner.client = client

    if start_with_debug:
        assert debug_port_future is not None

        # avoid blocking main thread?
        debug_async_future = asyncio.wrap_future(future=debug_port_future)
        try:
            await asyncio.wait_for(debug_async_future, timeout=30)
        except TimeoutError as exception:
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            raise RunnerFailedToStart(f"Failed to get debugger port in 30 seconds: {runner.readable_id}") from exception
        
        debug_port = debug_async_future.result()
        logger.info(f"debug port: {debug_port}")

        if start_debug_session is not None:
            debug_params = {
                "name": "Python: WM",
                "type": "debugpy",
                "request": "attach",
                "connect": {
                    "host": "localhost",
                    "port": debug_port
                },
                "justMyCode": False,
                # "logToFile": True,
            }
            await start_debug_session(debug_params)

        try:
            await client.connect_to_server(io_thread=ws_context.runner_io_thread, timeout=None)
        except Exception as exception: # TODO: analyze which can occur
            # TODO: analyze whether server process will always stop if connection
            logger.error(f"Runner {runner.readable_id} failed to connect to server: {exception}")
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            raise RunnerFailedToStart(str(exception)) from exception

    async def on_exit():
        logger.debug(f"Extension Runner {runner.readable_id} exited")
        runner.status = runner_client.RunnerStatus.EXITED
        await notify_project_changed(
            ws_context.ws_projects[runner.working_dir_path]
        )  # TODO: fix
        # TODO: restart if WM is not stopping

    runner.client.server_exit_callback = on_exit

    runner.client.feature(
        _internal_client_types.WORKSPACE_APPLY_EDIT, _apply_workspace_edit
    )

    async def on_progress(params: _internal_client_types.ProgressParams) -> None:
        logger.debug(f"Got progress from runner for token: {params.token}")
        try:
            result_value = json.loads(params.value)
        except json.JSONDecodeError as exception:
            logger.error(f"Failed to decode partial result value json: {exception}")
            return

        partial_result = domain.PartialResult(
            token=params.token, value=result_value
        )
        partial_results.publish(partial_result)

    runner.client.feature(_internal_client_types.PROGRESS, on_progress)

    async def get_project_raw_config(
        params: _internal_client_types.GetProjectRawConfigParams,
    ):
        logger.debug(f"Get project raw config: {params}")
        project_def_path_str = params.project_def_path
        project_def_path = Path(project_def_path_str)
        try:
            project_raw_config = ws_context.ws_projects_raw_configs[
                project_def_path.parent
            ]
        except KeyError as exception:
            raise ValueError(f"Config of project '{project_def_path_str}' not found") from exception
        return _internal_client_types.GetProjectRawConfigResult(
            config=json.dumps(project_raw_config)
        )

    runner.client.feature(
        _internal_client_types.PROJECT_RAW_CONFIG_GET,
        get_project_raw_config,
    )


async def stop_extension_runner(runner: runner_client.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.readable_id}")
    if runner.status == runner_client.RunnerStatus.RUNNING:
        try:
            await _internal_client_api.shutdown(client=runner.client)
        except Exception as e:
            logger.error(f"Failed to shutdown {runner.readable_id}:")
            logger.exception(e)

        await _internal_client_api.exit(client=runner.client)
        logger.trace(f"Stopped extension runner {runner.readable_id}")
    else:
        logger.trace("Extension runner was not running")


def stop_extension_runner_sync(runner: runner_client.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.readable_id}")
    if runner.status == runner_client.RunnerStatus.RUNNING:
        try:
            _internal_client_api.shutdown_sync(client=runner.client)
        except Exception as e:
            logger.error(f"Failed to shutdown:")
            logger.exception(e)

        _internal_client_api.exit_sync(runner.client)
        logger.trace(f"Stopped extension runner {runner.readable_id}")
    else:
        logger.trace("Extension runner was not running")


async def start_runners_with_presets(
    projects: list[domain.Project], ws_context: context.WorkspaceContext
) -> None:
    # start runners with presets in projects, resolve presets and read project actions
    new_runners_tasks: list[asyncio.Task] = []
    try:
        # first start runner in 'dev_workspace' env to be able to resolve presets for
        # other envs (presets can be currently only in `dev_workspace` env)
        async with asyncio.TaskGroup() as tg:
            for project in projects:
                project_status = project.status
                if project_status == domain.ProjectStatus.CONFIG_VALID:
                    # first check whether runner doesn't exist yet to avoid duplicates
                    project_runners = ws_context.ws_projects_extension_runners.get(
                        project.dir_path, {}
                    )
                    project_dev_workspace_runner = project_runners.get(
                        "dev_workspace", None
                    )
                    start_new_runner = True
                    if (
                        project_dev_workspace_runner is not None
                        and project_dev_workspace_runner.status
                        in [
                            runner_client.RunnerStatus.INITIALIZING,
                            runner_client.RunnerStatus.RUNNING,
                        ]
                    ):
                        # start a new one only if:
                        # - either there is no runner yet
                        # or venv exist(=exclude `runner_client.RunnerStatus.NO_VENV`)
                        #    and runner is not initializing or running already
                        start_new_runner = False

                    if start_new_runner:
                        task = tg.create_task(
                            _start_dev_workspace_runner(
                                project_def=project, ws_context=ws_context
                            )
                        )
                        new_runners_tasks.append(task)
                elif project_status != domain.ProjectStatus.NO_FINECODE:
                    raise jsonrpc_client.RunnerFailedToStart(
                        f"Project '{project.name}' has invalid configuration, status: {project_status.name}"
                    )

        save_runners_from_tasks_in_context(
            tasks=new_runners_tasks, ws_context=ws_context
        )
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            if isinstance(
                exception, jsonrpc_client.BaseRunnerRequestException
            ) or isinstance(exception, jsonrpc_client.RunnerFailedToStart):
                logger.error(exception.message)
            else:
                logger.error("Unexpected exception:")
                logger.exception(exception)
        raise jsonrpc_client.RunnerFailedToStart(
            "Failed to initialize runner(s). See previous logs for more details"
        )

    for project in projects:
        if project.status != domain.ProjectStatus.CONFIG_VALID:
            continue

        try:
            await read_configs.read_project_config(
                project=project, ws_context=ws_context
            )
            collect_actions.collect_actions(
                project_path=project.dir_path, ws_context=ws_context
            )
        except config_models.ConfigurationError as exception:
            raise jsonrpc_client.RunnerFailedToStart(
                f"Reading project config with presets and collecting actions in {project.dir_path} failed: {exception.message}"
            ) from exception

        # update config of dev_workspace runner, the new config contains resolved presets
        dev_workspace_runner = ws_context.ws_projects_extension_runners[
            project.dir_path
        ]["dev_workspace"]
        await update_runner_config(runner=dev_workspace_runner, project=project)


async def get_or_start_runners_with_presets(
    project_dir_path: Path, ws_context: context.WorkspaceContext
) -> runner_client.ExtensionRunnerInfo:
    # project is expected to have status `ProjectStatus.CONFIG_VALID`
    has_dev_workspace_runner = (
        "dev_workspace" in ws_context.ws_projects_extension_runners[project_dir_path]
    )
    if not has_dev_workspace_runner:
        project = ws_context.ws_projects[project_dir_path]
        await start_runners_with_presets([project], ws_context)
    dev_workspace_runner = ws_context.ws_projects_extension_runners[project_dir_path][
        "dev_workspace"
    ]
    if dev_workspace_runner.status == runner_client.RunnerStatus.RUNNING:
        return dev_workspace_runner
    elif dev_workspace_runner.status == runner_client.RunnerStatus.INITIALIZING:
        await dev_workspace_runner.initialized_event.wait()
        return dev_workspace_runner
    else:
        raise jsonrpc_client.RunnerFailedToStart(
            f"Status of dev_workspace runner: {dev_workspace_runner.status}, logs: {dev_workspace_runner.logs_path}"
        )


async def start_runner(
    project_def: domain.Project, env_name: str, ws_context: context.WorkspaceContext, debug: bool = False
) -> runner_client.ExtensionRunnerInfo:
    # this function manages status of the runner and initialized event
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=project_def.dir_path,
        env_name=env_name,
        status=runner_client.RunnerStatus.INITIALIZING,
        initialized_event=asyncio.Event(),
        client=None,
    )
    save_runner_in_context(runner=runner, ws_context=ws_context)
    await _start_extension_runner_process(runner=runner, ws_context=ws_context, debug=debug)

    try:
        await _init_lsp_client(runner=runner, project=project_def)
    except jsonrpc_client.RunnerFailedToStart as exception:
        runner.status = runner_client.RunnerStatus.FAILED
        await notify_project_changed(project_def)
        runner.initialized_event.set()
        raise exception

    if (
        project_def.dir_path not in ws_context.ws_projects_raw_configs
        or project_def.actions is None
    ):
        try:
            await read_configs.read_project_config(
                project=project_def, ws_context=ws_context
            )
            collect_actions.collect_actions(
                project_path=project_def.dir_path, ws_context=ws_context
            )
        except config_models.ConfigurationError as exception:
            runner.status = runner_client.RunnerStatus.FAILED
            runner.initialized_event.set()
            await notify_project_changed(project_def)
            raise jsonrpc_client.RunnerFailedToStart(
                f"Found problem in configuration of {project_def.dir_path}: {exception.message}"
            ) from exception

    await update_runner_config(runner=runner, project=project_def)
    await _finish_runner_init(runner=runner, project=project_def, ws_context=ws_context)

    runner.status = runner_client.RunnerStatus.RUNNING
    await notify_project_changed(project_def)
    runner.initialized_event.set()

    return runner


async def get_or_start_runner(
    project_def: domain.Project, env_name: str, ws_context: context.WorkspaceContext
) -> runner_client.ExtensionRunnerInfo:
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[project_def.dir_path]
        runner = runners_by_env[env_name]
        logger.trace(f"Runner {runner.readable_id} found")
    except KeyError:
        logger.trace(
            f"Runner for env {env_name} in {project_def.dir_path} not found, start one"
        )
        runner = await start_runner(
            project_def=project_def, env_name=env_name, ws_context=ws_context
        )

    if runner.status != runner_client.RunnerStatus.RUNNING:
        runner_error = False
        if runner.status == runner_client.RunnerStatus.INITIALIZING:
            logger.trace(f"Runner {runner.readable_id} is initializing, wait for it")
            await runner.initialized_event.wait()
            if runner.status != runner_client.RunnerStatus.RUNNING:
                runner_error = True
        else:
            runner_error = True

        if runner_error:
            raise jsonrpc_client.RunnerFailedToStart(
                f"Runner {env_name} in project {project_def.dir_path} is not running. Status: {runner.status}"
            )

    return runner


async def _start_dev_workspace_runner(
    project_def: domain.Project, ws_context: context.WorkspaceContext
) -> runner_client.ExtensionRunnerInfo:
    return await get_or_start_runner(
        project_def=project_def, env_name="dev_workspace", ws_context=ws_context
    )


async def _init_lsp_client(
    runner: runner_client.ExtensionRunnerInfo, project: domain.Project
) -> None:
    try:
        await _internal_client_api.initialize(
            client=runner.client,
            client_process_id=os.getpid(),
            client_name="FineCode_WorkspaceManager",
            client_version="0.1.0",
            client_workspace_dir=runner.working_dir_path
        )
    except jsonrpc_client.BaseRunnerRequestException as exception:
        raise jsonrpc_client.RunnerFailedToStart(
            f"Runner failed to initialize: {exception.message}"
        ) from exception

    try:
        await _internal_client_api.notify_initialized(runner.client)
    except Exception as exception:
        logger.error(f"Failed to notify runner about initialization: {exception}")
        logger.exception(exception)
        raise jsonrpc_client.RunnerFailedToStart(
            f"Runner failed to notify about initialization: {exception}"
        ) from exception

    logger.debug(f"LSP Client for initialized: {runner.readable_id}")


async def update_runner_config(
    runner: runner_client.ExtensionRunnerInfo, project: domain.Project
) -> None:
    assert project.actions is not None
    config = runner_client.RunnerConfig(
        actions=project.actions, action_handler_configs=project.action_handler_configs
    )
    try:
        await runner_client.update_config(runner, project.def_path, config)
    except jsonrpc_client.BaseRunnerRequestException as exception:
        runner.status = runner_client.RunnerStatus.FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        raise jsonrpc_client.RunnerFailedToStart(
            f"Runner failed to update config: {exception.message}"
        ) from exception

    logger.debug(f"Updated config of runner {runner.readable_id}")


async def _finish_runner_init(
    runner: runner_client.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # TODO: save per runner only during initialization. But where to get data from
    #       in case of runner restart?
    await send_opened_files(
        runner=runner, opened_files=list(ws_context.opened_documents.values())
    )


def save_runners_from_tasks_in_context(
    tasks: list[asyncio.Task[runner_client.ExtensionRunnerInfo]], ws_context: context.WorkspaceContext
) -> None:
    extension_runners: list[runner_client.ExtensionRunnerInfo] = [
        runner.result() for runner in tasks if runner is not None
    ]

    for new_runner in extension_runners:
        save_runner_in_context(runner=new_runner, ws_context=ws_context)


def save_runner_in_context(
    runner: runner_client.ExtensionRunnerInfo, ws_context: context.WorkspaceContext
) -> None:
    if runner.working_dir_path not in ws_context.ws_projects_extension_runners:
        ws_context.ws_projects_extension_runners[runner.working_dir_path] = {}
    ws_context.ws_projects_extension_runners[runner.working_dir_path][
        runner.env_name
    ] = runner


async def send_opened_files(
    runner: runner_client.ExtensionRunnerInfo,
    opened_files: list[domain.TextDocumentInfo],
):
    files_for_runner: list[domain.TextDocumentInfo] = []
    for opened_file_info in opened_files:
        file_path = Path(opened_file_info.uri.replace("file://", ""))
        if not file_path.is_relative_to(runner.working_dir_path):
            continue
        else:
            files_for_runner.append(opened_file_info)

    try:
        async with asyncio.TaskGroup() as tg:
            for file_info in files_for_runner:
                tg.create_task(
                    runner_client.notify_document_did_open(
                        runner=runner,
                        document_info=domain.TextDocumentInfo(
                            uri=file_info.uri, version=file_info.version
                        ),
                    )
                )
    except ExceptionGroup as eg:
        logger.error(f"Error while sending opened document: {eg.exceptions}")


async def check_runner(runner_dir: Path, env_name: str) -> bool:
    try:
        python_cmd = finecode_cmd.get_python_cmd(runner_dir, env_name)
    except ValueError:
        logger.debug(f"No venv for {env_name} of {runner_dir}")
        # no venv
        return False

    # get version of extension runner. If it works and we get valid
    # value, assume extension runner works correctly
    cmd = f"{python_cmd} -m finecode_extension_runner.cli version"
    logger.debug(f"Run '{cmd}' in {runner_dir}")
    async_subprocess = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=runner_dir,
    )
    try:
        raw_stdout, raw_stderr = await asyncio.wait_for(
            async_subprocess.communicate(), timeout=5
        )
    except TimeoutError:
        logger.debug(f"Timeout 5 sec({runner_dir})")
        return False

    if async_subprocess.returncode != 0:
        logger.debug(
            f"Return code: {async_subprocess.returncode}, stderr: {raw_stderr.decode()}"
        )
        return False

    stdout = raw_stdout.decode()
    return "FineCode Extension Runner " in stdout


def remove_runner_venv(runner_dir: Path, env_name: str) -> None:
    venv_dir_path = finecode_cmd.get_venv_dir_path(
        project_path=runner_dir, env_name=env_name
    )
    if venv_dir_path.exists():
        logger.debug(f"Remove venv {venv_dir_path}")
        shutil.rmtree(venv_dir_path)


async def restart_extension_runners(
    runner_working_dir_path: Path, ws_context: context.WorkspaceContext
) -> None:
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    # TODO: parallel?
    for runner in runners_by_env.values():
        await restart_extension_runner(runner_working_dir_path=runner.working_dir_path, env_name=runner.env_name, ws_context=ws_context)


async def restart_extension_runner(
    runner_working_dir_path: Path, env_name: str, ws_context: context.WorkspaceContext, debug: bool = False
) -> None:
    # TODO: reload config?
    try:
        runners_by_env = ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    try:
        runner = runners_by_env[env_name]
    except KeyError:
        logger.error(f"Cannot find runner for env {env_name} in {runner_working_dir_path}")
        return

    await stop_extension_runner(runner)

    project_def = ws_context.ws_projects[runner.working_dir_path]
    await start_runner(
        project_def=project_def,
        env_name=runner.env_name,
        ws_context=ws_context,
        debug=debug
    )