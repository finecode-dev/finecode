from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Sequence

import janus
from loguru import logger
from lsprotocol import types

import finecode.workspace_manager.domain as domain
import finecode.workspace_manager.context as context
import finecode.workspace_manager.api as manager_api
import finecode.workspace_manager.finecode_cmd as finecode_cmd
from finecode.workspace_manager.runner_lsp_client import create_lsp_client_io
from finecode.workspace_manager.server.lsp_server import create_lsp_server
from modapp.extras.logs import save_logs_to_file
from modapp.extras.platformdirs import get_dirs
import finecode.pygls_utils as pygls_utils
import finecode.communication_utils as communication_utils


async def start(comm_type: communication_utils.CommunicationType, host: str | None = None, port: int | None = None, trace: bool = False) -> None:
    log_dir_path = Path(get_dirs(app_name='FineCode_Workspace_Manager', app_author='FineCode', version='1.0').user_log_dir)
    logger.remove()
    save_logs_to_file(file_path=log_dir_path / 'execution.log', log_level="TRACE" if trace else "INFO", stdout=False)

    server = create_lsp_server()
    if comm_type == communication_utils.CommunicationType.TCP:
        if host is None or port is None:
            raise ValueError("TCP server requires host and port to be provided.")

        await pygls_utils.start_tcp_async(server, host, port)
    elif comm_type == communication_utils.CommunicationType.WS:
        if host is None or port is None:
            raise ValueError("WS server requires host and port to be provided.")
        raise NotImplementedError()  # async version of start_ws is needed
    else:
        await pygls_utils.start_io_async(server)


async def start_in_ws_context(ws_context: context.WorkspaceContext) -> None:
    # one for all, doesn't need to change on ws dirs change
    asyncio.create_task(handle_runners_lifecycle(ws_context))


async def update_runners(ws_context: context.WorkspaceContext) -> None:
    extension_runners = list(ws_context.ws_projects_extension_runners.values())
    new_dirs, deleted_dirs = _find_changed_dirs(
        [*ws_context.ws_projects.keys()], [runner.working_dir_path for runner in extension_runners]
    )
    for deleted_dir in deleted_dirs:
        try:
            runner_to_delete = next(
                runner for runner in extension_runners if runner.working_dir_path == deleted_dir
            )
        except StopIteration:
            continue
        stop_extension_runner(runner_to_delete)
        extension_runners.remove(runner_to_delete)

    new_runners_coros = [
        start_extension_runner(runner_dir=new_dir, ws_context=ws_context) for new_dir in new_dirs
    ]
    new_runners = await asyncio.gather(*new_runners_coros)
    extension_runners += [runner for runner in new_runners if runner is not None]

    ws_context.ws_projects_extension_runners = {
        runner.working_dir_path: runner for runner in extension_runners
    }


async def handle_runners_lifecycle(ws_context: context.WorkspaceContext):
    await update_runners(ws_context)
    try:
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        logger.exception(e)
    finally:
        # TODO: stop all log handlers?
        for runner in ws_context.ws_projects_extension_runners.values():
            stop_extension_runner(runner)

        ws_context.ws_projects_extension_runners.clear()


def _find_changed_dirs(
    new_dirs: Sequence[Path], old_dirs: Sequence[Path]
) -> tuple[list[Path], list[Path]]:
    added_dirs: list[Path] = []
    deleted_dirs: list[Path] = []
    for new_dir in new_dirs:
        if new_dir not in old_dirs:
            added_dirs.append(new_dir)
    for old_dir in old_dirs:
        if old_dir not in new_dirs:
            deleted_dirs.append(old_dir)

    return added_dirs, deleted_dirs


async def start_extension_runner(
    runner_dir: Path, ws_context: context.WorkspaceContext
) -> manager_api.ExtensionRunnerInfo | None:
    runner_info = manager_api.ExtensionRunnerInfo(
        process_id=0,
        working_dir_path=runner_dir,
        output_queue=janus.Queue(),
        started_event=asyncio.Event(),
        process_future=None,
        stop_event=threading.Event(),
    )

    try:
        _finecode_cmd = finecode_cmd.get_finecode_cmd(runner_dir)
    except ValueError:
        try:
            ws_context.ws_projects[runner_dir].status = domain.ProjectStatus.NO_FINECODE_SH
        except KeyError:
            ...
        return None

    # temporary remove VIRTUAL_ENV env variable to avoid starting in wrong venv
    old_virtual_env_var = os.environ.get("VIRTUAL_ENV", "")
    os.environ["VIRTUAL_ENV"] = ""
    runner_info.client = await create_lsp_client_io(f"{_finecode_cmd} -m finecode.extension_runner.cli --trace")
    os.environ["VIRTUAL_ENV"] = old_virtual_env_var
    await init_runner(runner_info, ws_context.ws_projects[runner_dir], ws_context.ws_projects_raw_configs[runner_dir])
    return runner_info


def stop_extension_runner(runner: manager_api.ExtensionRunnerInfo) -> None:
    runner.stop_event.set()
    if runner.process_future is not None:
        # wait for end of the process to make sure it was stopped
        runner.process_future.result()
    logger.trace(f"Stop extension runner {runner.process_id} in {runner.working_dir_path}")


def get_subactions(
    names: list[str], project_raw_config: dict[str, Any]
) -> list[domain.Action]:
    subactions: list[domain.Action] = []
    for name in names:
        try:
            action_raw = project_raw_config["tool"]["finecode"]["action"][name]
        except KeyError:
            raise ValueError("Action definition not found")
        try:
            subactions.append(domain.Action(name=name, source=action_raw["source"]))
        except KeyError:
            raise ValueError("Action has no source")

    return subactions


async def init_runner(runner: manager_api.ExtensionRunnerInfo, project: domain.Project, project_raw_config: dict[str, Any]) -> None:
    # initialization is required to be able to perform other requests
    assert runner.client is not None
    try:
        await asyncio.wait_for(runner.client.protocol.send_request_async(method=types.INITIALIZE, params=types.InitializeParams(process_id=os.getpid(), capabilities=types.ClientCapabilities(), client_info=types.ClientInfo(name='FineCode_WorkspaceManager', version='0.1.0'), trace=types.TraceValue.Verbose)), 10)
    except Exception as e:
        logger.exception(e)
        return

    try:
        runner.client.protocol.notify(method=types.INITIALIZED, params=types.InitializedParams())
    except Exception as e:
        logger.exception(e)
        return
    logger.debug("LSP Server initialized")

    assert project.actions is not None
    all_actions = set([])
    actions_to_process = set(project.actions)
    while len(actions_to_process) > 0:
        action = actions_to_process.pop()
        all_actions.add(action)
        actions_to_process |= set(get_subactions(names=action.subactions, project_raw_config=project_raw_config))
    all_actions_dict = {action.name: action for action in all_actions}

    try:
        # lsp client requests have no timeout, add own one
        try:
            await asyncio.wait_for(runner.client.protocol.send_request_async(
                method=types.WORKSPACE_EXECUTE_COMMAND,
                params=types.ExecuteCommandParams(command='finecodeRunner/updateConfig', arguments=[
                    runner.working_dir_path.as_posix(),
                    runner.working_dir_path.stem,
                    all_actions_dict,
                    project.actions_configs
                ])
            ), 10)
        except TimeoutError:
            logger.error(f"Failed to update config of runner {runner.working_dir_path}")
        
        logger.debug(f"Updated config of runner {runner.working_dir_path}, process id {runner.process_id}")
        runner.started_event.set()
    except Exception as e:
        # TODO: set project status to appropriate error
        logger.exception(e)
