import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Coroutine

from loguru import logger
from lsprotocol import types

from finecode import dirs_utils
from finecode.pygls_client_utils import create_lsp_client_io
from finecode.workspace_manager import context, domain, finecode_cmd
from finecode.workspace_manager.config import collect_actions, read_configs
from finecode.workspace_manager.runner import runner_client, runner_info
from finecode.workspace_manager.utils import iterable_subscribe

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
    runner_dir: Path, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo | None:
    try:
        _finecode_cmd = finecode_cmd.get_finecode_cmd(runner_dir)
    except ValueError:
        try:
            ws_context.ws_projects[runner_dir].status = (
                domain.ProjectStatus.NO_FINECODE_SH
            )
            await notify_project_changed(ws_context.ws_projects[runner_dir])
        except KeyError:
            ...
        return None

    process_args: list[str] = [
        "--trace",
        f"--project-path={runner_dir.as_posix()}",
    ]
    # TODO: config parameter for debug and debug port
    # if runner_dir == Path("/home/user/Development/FineCode/finecode"):
    #     process_args.append("--debug")
    #     process_args.append("--debug-port=5681")

    process_args_str: str = " ".join(process_args)
    client = await create_lsp_client_io(
        runner_info.CustomJsonRpcClient,
        f"{_finecode_cmd} -m finecode.extension_runner.cli {process_args_str}",
        runner_dir,
    )
    runner_info_instance = runner_info.ExtensionRunnerInfo(
        working_dir_path=runner_dir, initialized_event=asyncio.Event(), client=client
    )

    async def on_exit():
        logger.debug(f"Extension Runner {runner_info_instance.working_dir_path} exited")
        ws_context.ws_projects[runner_dir].status = domain.ProjectStatus.EXITED
        await notify_project_changed(ws_context.ws_projects[runner_dir])
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

    return runner_info_instance


async def stop_extension_runner(runner: runner_info.ExtensionRunnerInfo) -> None:
    logger.trace(f"Trying to stop extension runner {runner.working_dir_path}")
    if not runner.client.stopped:
        logger.debug("Send shutdown to server")
        try:
            await runner_client.shutdown(runner=runner)
        except Exception as e:
            # TODO: handle
            logger.error(f"Failed to shutdown {e}")

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
            # TODO: handle
            logger.error(f"Failed to shutdown {e}")

        runner_client.exit_sync(runner)
        logger.debug("Sent exit to server")
        logger.trace(
            f"Stop extension runner {runner.process_id}"
            f" in {runner.working_dir_path}"
        )
    else:
        logger.trace("Extension runner was not running")


async def kill_extension_runner(runner: runner_info.ExtensionRunnerInfo) -> None:
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
    extension_runners = list(ws_context.ws_projects_extension_runners.values())
    new_dirs, deleted_dirs = dirs_utils.find_changed_dirs(
        [*ws_context.ws_projects.keys()],
        [runner.working_dir_path for runner in extension_runners],
    )
    for deleted_dir in deleted_dirs:
        try:
            runner_to_delete = next(
                runner
                for runner in extension_runners
                if runner.working_dir_path == deleted_dir
            )
        except StopIteration:
            continue
        await stop_extension_runner(runner_to_delete)
        extension_runners.remove(runner_to_delete)

    new_runners_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for new_dir in new_dirs:
                project = ws_context.ws_projects[new_dir]
                project_status = project.status
                if project_status == domain.ProjectStatus.READY:
                    runner_task = tg.create_task(start_extension_runner(runner_dir=new_dir, ws_context=ws_context))
                    new_runners_tasks.append(runner_task)
                elif project_status != domain.ProjectStatus.NO_FINECODE:
                    raise RunnerFailedToStart(f"Runner for project '{project.name}' failed to start, status: {project_status.name}")
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            if isinstance(exception, runner_client.BaseRunnerRequestException) or isinstance(exception, RunnerFailedToStart):
                logger.error(exception.message)
            else:
                logger.exception(exception)
        raise RunnerFailedToStart("Failed to start runner")

    extension_runners += [
        runner.result() for runner in new_runners_tasks if runner is not None
    ]

    ws_context.ws_projects_extension_runners = {
        runner.working_dir_path: runner for runner in extension_runners
    }

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
                logger.exception(exception)
        raise RunnerFailedToStart("Failed to initialize runner")


async def _init_runner(
    runner: runner_info.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # initialization is required to be able to perform other requests
    logger.trace(f"Init runner {runner.working_dir_path}")
    try:
        await runner_client.initialize(
            runner,
            client_process_id=os.getpid(),
            client_name="FineCode_WorkspaceManager",
            client_version="0.1.0",
        )
    except runner_client.BaseRunnerRequestException as error:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        raise RunnerFailedToStart(f"Runner failed to initialize: {error.message}")

    try:
        await runner_client.notify_initialized(runner)
    except Exception as error:
        logger.error(f"Failed to notify runner about initialization: {error}")
        project.status = domain.ProjectStatus.RUNNER_FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        logger.exception(error)
        raise RunnerFailedToStart(
            f"Runner failed to notify about initialization: {error}"
        )

    logger.debug("LSP Server initialized")

    await read_configs.read_project_config(project=project, ws_context=ws_context)
    collect_actions.collect_actions(
        project_path=project.dir_path, ws_context=ws_context
    )

    assert (
        project.actions is not None
    ), f"Actions of project {project.dir_path} are not read yet"

    try:
        await runner_client.update_config(runner, project.actions)
    except runner_client.BaseRunnerRequestException as exception:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        await notify_project_changed(project)
        runner.initialized_event.set()
        raise RunnerFailedToStart(f"Runner failed to update config: {exception.message}")

    logger.debug(
        f"Updated config of runner {runner.working_dir_path},"
        f" process id {runner.process_id}"
    )
    project.status = domain.ProjectStatus.RUNNING
    await notify_project_changed(project)

    await send_opened_files(
        runner=runner, opened_files=list(ws_context.opened_documents.values())
    )

    runner.initialized_event.set()


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
