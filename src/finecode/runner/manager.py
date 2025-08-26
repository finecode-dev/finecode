import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Callable, Coroutine

from loguru import logger
from lsprotocol import types

from finecode import context, dirs_utils, domain, finecode_cmd
from finecode.config import collect_actions, config_models, read_configs
from finecode.pygls_client_utils import create_lsp_client_io
from finecode.runner import runner_client, runner_info
from finecode.utils import iterable_subscribe

project_changed_callback: (
    Callable[[domain.Project], Coroutine[None, None, None]] | None
) = None
get_document: Callable[[], Coroutine] | None = None
apply_workspace_edit: Callable[[], Coroutine] | None = None
partial_results: iterable_subscribe.IterableSubscribe = (
    iterable_subscribe.IterableSubscribe()
)


class RunnerFailedToStart(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def notify_project_changed(project: domain.Project) -> None:
    if project_changed_callback is not None:
        await project_changed_callback(project)


async def _apply_workspace_edit(params: types.ApplyWorkspaceEditParams):
    def map_change_object(change):
        return types.TextEdit(
            range=types.Range(
                start=types.Position(
                    line=change.range.start.line, character=change.range.start.character
                ),
                end=types.Position(
                    change.range.end.line, character=change.range.end.character
                ),
            ),
            new_text=change.newText,
        )

    converted_params = types.ApplyWorkspaceEditParams(
        edit=types.WorkspaceEdit(
            document_changes=[
                types.TextDocumentEdit(
                    text_document=types.OptionalVersionedTextDocumentIdentifier(
                        document_edit.textDocument.uri
                    ),
                    edits=[map_change_object(change) for change in document_edit.edits],
                )
                for document_edit in params.edit.documentChanges
            ]
        )
    )
    return await apply_workspace_edit(converted_params)


async def start_extension_runner(
    runner_dir: Path, env_name: str, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo | None:
    runner_info_instance = runner_info.ExtensionRunnerInfo(
        working_dir_path=runner_dir,
        env_name=env_name,
        status=runner_info.RunnerStatus.READY_TO_START,
        initialized_event=asyncio.Event(),
        client=None,
    )

    try:
        python_cmd = finecode_cmd.get_python_cmd(runner_dir, env_name)
    except ValueError:
        try:
            runner_info_instance.status = runner_info.RunnerStatus.NO_VENV
            await notify_project_changed(ws_context.ws_projects[runner_dir])
        except KeyError:
            ...
        logger.error(
            f"Project {runner_dir} uses finecode, but env (venv) doesn't exist yet. Run `prepare_env` command to create it"
        )
        return None

    process_args: list[str] = [
        "--trace",
        f"--project-path={runner_dir.as_posix()}",
        f"--env-name={env_name}",
    ]
    env_config = ws_context.ws_projects[runner_dir].env_configs[env_name]
    runner_config = env_config.runner_config
    # TODO: also check whether lsp server is available, without it doesn't make sense
    # to start with debugger
    if runner_config.debug:
        process_args.append("--debug")
        # TODO: find free port and pass it
        process_args.append("--debug-port=5681")

    process_args_str: str = " ".join(process_args)
    client = await create_lsp_client_io(
        runner_info.CustomJsonRpcClient,
        f"{python_cmd} -m finecode_extension_runner.cli start {process_args_str}",
        runner_dir,
    )
    runner_info_instance.client = client
    # TODO: recognize started debugger and send command to lsp server

    async def on_exit():
        logger.debug(f"Extension Runner {runner_info_instance.working_dir_path} exited")
        runner_info_instance.status = runner_info.RunnerStatus.EXITED
        await notify_project_changed(ws_context.ws_projects[runner_dir])  # TODO: fix
        # TODO: restart if WM is not stopping

    runner_info_instance.client.server_exit_callback = on_exit

    if get_document is not None:
        register_get_document_feature = runner_info_instance.client.feature(
            "documents/get"
        )
        register_get_document_feature(get_document)

    register_workspace_apply_edit = runner_info_instance.client.feature(
        types.WORKSPACE_APPLY_EDIT
    )
    register_workspace_apply_edit(_apply_workspace_edit)

    async def on_progress(params: types.ProgressParams):
        logger.debug(f"Got progress from runner for token: {params.token}")
        partial_result = domain.PartialResult(
            token=params.token, value=json.loads(params.value)
        )
        partial_results.publish(partial_result)

    register_progress_feature = runner_info_instance.client.feature(types.PROGRESS)
    register_progress_feature(on_progress)

    async def get_project_raw_config(params):
        project_def_path_str = params.projectDefPath
        project_def_path = Path(project_def_path_str)
        try:
            project_raw_config = ws_context.ws_projects_raw_configs[
                project_def_path.parent
            ]
        except KeyError:
            raise ValueError(f"Config of project '{project_def_path_str}' not found")
        return {"config": json.dumps(project_raw_config)}

    register_get_project_raw_config_feature = runner_info_instance.client.feature(
        "projects/getRawConfig"
    )
    register_get_project_raw_config_feature(get_project_raw_config)

    return runner_info_instance


async def stop_extension_runner(runner: runner_info.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.working_dir_path}")
    if not runner.client.stopped:
        logger.debug("Send shutdown to server")
        try:
            await runner_client.shutdown(runner=runner)
        except Exception as e:
            logger.error(f"Failed to shutdown: {e}")

        await runner_client.exit(runner)
        logger.debug("Sent exit to server")
        await runner.client.stop()
        logger.trace(
            f"Stop extension runner {runner.process_id}"
            f" in {runner.working_dir_path}"
        )
    else:
        logger.trace("Extension runner was not running")


def stop_extension_runner_sync(runner: runner_info.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.working_dir_path}")
    if not runner.client.stopped:
        logger.debug("Send shutdown to server")
        try:
            runner_client.shutdown_sync(runner=runner)
        except Exception as e:
            # currently we get (almost?) always this error. TODO: Investigate why
            # mute for now to make output less verbose
            # logger.error(f"Failed to shutdown: {e}")
            ...

        runner_client.exit_sync(runner)
        logger.debug("Sent exit to server")
        logger.trace(
            f"Stop extension runner {runner.process_id}"
            f" in {runner.working_dir_path}"
        )
    else:
        logger.trace("Extension runner was not running")


async def kill_extension_runner(runner: runner_info.ExtensionRunnerInfo) -> None:
    if runner.client is not None:
        if runner.client._server is not None:
            runner.client._server.terminate()
        await runner.client.stop()


async def update_runners(ws_context: context.WorkspaceContext) -> None:
    # starts runners for new(=which don't have runner yet) projects in `ws_context`
    # and stops runners for projects which are not in `ws_context` anymore
    #
    # during initialization of new runners it also reads their configurations and
    # actions
    #
    # this function should handle all possible statuses of projects and they either
    # start of fail to start, only projects without finecode are ignored
    extension_runners_paths = list(ws_context.ws_projects_extension_runners.keys())
    new_dirs, deleted_dirs = dirs_utils.find_changed_dirs(
        [*ws_context.ws_projects.keys()],
        extension_runners_paths,
    )
    for deleted_dir in deleted_dirs:
        runners_by_env = ws_context.ws_projects_extension_runners[deleted_dir]
        for runner in runners_by_env.values():
            await stop_extension_runner(runner)
        del ws_context.ws_projects_extension_runners[deleted_dir]

    projects = [ws_context.ws_projects[new_dir] for new_dir in new_dirs]
    # first start runners with presets to be able to resolve presets
    await start_runners_with_presets(projects, ws_context)

    new_runners_tasks: list[asyncio.Task] = []
    try:
        # only then start runners for all other envs
        new_runners_tasks = []
        async with asyncio.TaskGroup() as tg:
            for new_dir in new_dirs:
                project = ws_context.ws_projects[new_dir]
                project_status = project.status
                if (
                    ws_context.ws_projects_extension_runners.get(new_dir, {}).get(
                        "dev_no_runtime", None
                    )
                    is not None
                ):
                    # start only if dev_no_runtime started successfully
                    for env in project.envs:
                        if env == "dev_no_runtime":
                            # this env has already started above
                            continue

                        runner_task = tg.create_task(
                            start_extension_runner(
                                runner_dir=new_dir, env_name=env, ws_context=ws_context
                            )
                        )
                        new_runners_tasks.append(runner_task)

    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            if isinstance(
                exception, runner_client.BaseRunnerRequestException
            ) or isinstance(exception, RunnerFailedToStart):
                logger.error(exception.message)
            else:
                logger.error("Unexpected exception:")
                logger.exception(exception)
        raise RunnerFailedToStart("Failed to start runner")

    save_runners_from_tasks_in_context(tasks=new_runners_tasks, ws_context=ws_context)
    extension_runners: list[runner_info.ExtensionRunnerInfo] = [
        runner.result() for runner in new_runners_tasks if runner is not None
    ]

    try:
        async with asyncio.TaskGroup() as tg:
            for runner in extension_runners:
                tg.create_task(
                    _init_runner(
                        runner,
                        ws_context.ws_projects[runner.working_dir_path],
                        ws_context,
                    )
                )
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            if isinstance(exception, runner_client.BaseRunnerRequestException):
                logger.error(exception.message)
            else:
                logger.error("Unexpected exception:")
                logger.exception(exception)
        raise RunnerFailedToStart("Failed to initialize runner")


async def start_runners_with_presets(
    projects: list[domain.Project], ws_context: context.WorkspaceContext
) -> None:
    new_runners_tasks: list[asyncio.Task] = []
    try:
        # first start runner in 'dev_no_runtime' env to be able to resolve presets for
        # other envs (presets can be currently only in `dev_no_runtime` env)
        async with asyncio.TaskGroup() as tg:
            for project in projects:
                project_status = project.status
                if project_status == domain.ProjectStatus.CONFIG_VALID:
                    task = tg.create_task(
                        _start_dev_no_runtime_runner(
                            project_def=project, ws_context=ws_context
                        )
                    )
                    new_runners_tasks.append(task)
                elif project_status != domain.ProjectStatus.NO_FINECODE:
                    raise RunnerFailedToStart(
                        f"Project '{project.name}' has invalid configuration, status: {project_status.name}"
                    )

        save_runners_from_tasks_in_context(
            tasks=new_runners_tasks, ws_context=ws_context
        )
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            if isinstance(
                exception, runner_client.BaseRunnerRequestException
            ) or isinstance(exception, RunnerFailedToStart):
                logger.error(exception.message)
            else:
                logger.error("Unexpected exception:")
                logger.exception(exception)
        raise RunnerFailedToStart(
            "Failed to initialize runner(s). See previous logs for more details"
        )


async def start_runner(
    project_def: domain.Project, env_name: str, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo:
    runner = await start_extension_runner(
        runner_dir=project_def.dir_path, env_name=env_name, ws_context=ws_context
    )

    if runner is None:
        raise RunnerFailedToStart(
            f"Runner '{env_name}' in project {project_def.name} failed to start"
        )

    save_runner_in_context(runner=runner, ws_context=ws_context)

    # we cannot reuse '_init_runner' here because we need to start lsp client first,
    # read config(=also resolve presets) and only then we can update runner config,
    # because this requires resolved project config with presets
    await _init_lsp_client(runner=runner, project=project_def)

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
            raise RunnerFailedToStart(
                f"Found problem in configuration of {project_def.dir_path}: {exception.message}"
            )

    await update_runner_config(runner=runner, project=project_def)
    await _finish_runner_init(runner=runner, project=project_def, ws_context=ws_context)

    return runner


async def _start_dev_no_runtime_runner(
    project_def: domain.Project, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo:
    return await start_runner(
        project_def=project_def, env_name="dev_no_runtime", ws_context=ws_context
    )


async def _init_runner(
    runner: runner_info.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # initialization is required to be able to perform other requests
    logger.trace(f"Init runner {runner.working_dir_path}")
    assert project.actions is not None

    await _init_lsp_client(runner=runner, project=project)

    await update_runner_config(runner=runner, project=project)
    await _finish_runner_init(runner=runner, project=project, ws_context=ws_context)


async def _init_lsp_client(
    runner: runner_info.ExtensionRunnerInfo, project: domain.Project
) -> None:
    try:
        await runner_client.initialize(
            runner,
            client_process_id=os.getpid(),
            client_name="FineCode_WorkspaceManager",
            client_version="0.1.0",
        )
    except runner_client.BaseRunnerRequestException as error:
        runner.status = runner_info.RunnerStatus.FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        raise RunnerFailedToStart(f"Runner failed to initialize: {error.message}")

    try:
        await runner_client.notify_initialized(runner)
    except Exception as error:
        logger.error(f"Failed to notify runner about initialization: {error}")
        runner.status = runner_info.RunnerStatus.FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        logger.exception(error)
        raise RunnerFailedToStart(
            f"Runner failed to notify about initialization: {error}"
        )

    logger.debug("LSP Client initialized")


async def update_runner_config(
    runner: runner_info.ExtensionRunnerInfo, project: domain.Project
) -> None:
    assert project.actions is not None
    config = runner_client.RunnerConfig(
        actions=project.actions, action_handler_configs=project.action_handler_configs
    )
    try:
        await runner_client.update_config(runner, config)
    except runner_client.BaseRunnerRequestException as exception:
        runner.status = runner_info.RunnerStatus.FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        raise RunnerFailedToStart(
            f"Runner failed to update config: {exception.message}"
        )

    logger.debug(
        f"Updated config of runner {runner.working_dir_path},"
        f" process id {runner.process_id}"
    )


async def _finish_runner_init(
    runner: runner_info.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    runner.status = runner_info.RunnerStatus.RUNNING
    await notify_project_changed(project)

    await send_opened_files(
        runner=runner, opened_files=list(ws_context.opened_documents.values())
    )

    runner.initialized_event.set()


def save_runners_from_tasks_in_context(
    tasks: list[asyncio.Task], ws_context: context.WorkspaceContext
) -> None:
    extension_runners: list[runner_info.ExtensionRunnerInfo] = [
        runner.result() for runner in tasks if runner is not None
    ]

    for new_runner in extension_runners:
        save_runner_in_context(runner=new_runner, ws_context=ws_context)


def save_runner_in_context(
    runner: runner_info.ExtensionRunnerInfo, ws_context: context.WorkspaceContext
) -> None:
    if runner.working_dir_path not in ws_context.ws_projects_extension_runners:
        ws_context.ws_projects_extension_runners[runner.working_dir_path] = {}
    ws_context.ws_projects_extension_runners[runner.working_dir_path][
        runner.env_name
    ] = runner


async def send_opened_files(
    runner: runner_info.ExtensionRunnerInfo, opened_files: list[domain.TextDocumentInfo]
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
    except asyncio.TimeoutError:
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
