from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Sequence

import janus
from loguru import logger
from lsprotocol import types
from modapp.extras.logs import save_logs_to_file
from modapp.extras.platformdirs import get_dirs

import finecode.communication_utils as communication_utils
import finecode.pygls_utils as pygls_utils
import finecode.workspace_manager.collect_actions as collect_actions
import finecode.workspace_manager.context as context
import finecode.workspace_manager.domain as domain
import finecode.workspace_manager.finecode_cmd as finecode_cmd
import finecode.workspace_manager.read_configs as read_configs
import finecode.workspace_manager.runner_client as manager_api
from finecode.workspace_manager.create_lsp_client import create_lsp_client_io
from finecode.workspace_manager.server.lsp_server import create_lsp_server


async def start(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    log_dir_path = Path(
        get_dirs(
            app_name="FineCode_Workspace_Manager", app_author="FineCode", version="1.0"
        ).user_log_dir
    )
    logger.remove()
    save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=False,
    )

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
        # await pygls_utils.start_io_async(server)
        server.start_io()


def start_sync(
    comm_type: communication_utils.CommunicationType,
    host: str | None = None,
    port: int | None = None,
    trace: bool = False,
) -> None:
    log_dir_path = Path(
        get_dirs(
            app_name="FineCode_Workspace_Manager", app_author="FineCode", version="1.0"
        ).user_log_dir
    )
    logger.remove()
    save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=False,
    )

    server = create_lsp_server()
    server.start_io()


# async def start_in_ws_context(ws_context: context.WorkspaceContext) -> None:
#     # one for all, doesn't need to change on ws dirs change
#     asyncio.create_task(handle_runners_lifecycle(ws_context))


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
        await stop_extension_runner(runner_to_delete)
        extension_runners.remove(runner_to_delete)

    new_runners_coros = [
        start_extension_runner(runner_dir=new_dir, ws_context=ws_context)
        for new_dir in new_dirs
        if ws_context.ws_projects[new_dir].status == domain.ProjectStatus.READY
    ]
    new_runners = await asyncio.gather(*new_runners_coros)
    extension_runners += [runner for runner in new_runners if runner is not None]

    ws_context.ws_projects_extension_runners = {
        runner.working_dir_path: runner for runner in extension_runners
    }

    init_runners_coros = [
        init_runner(runner, ws_context.ws_projects[runner.working_dir_path], ws_context)
        for runner in extension_runners
    ]
    await asyncio.gather(*init_runners_coros)


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
        working_dir_path=runner_dir,
        output_queue=janus.Queue(),
        initialized_event=asyncio.Event(),
    )

    try:
        _finecode_cmd = finecode_cmd.get_finecode_cmd(runner_dir)
    except ValueError:
        try:
            ws_context.ws_projects[runner_dir].status = domain.ProjectStatus.NO_FINECODE_SH
        except KeyError:
            ...
        return None

    runner_info.client = await create_lsp_client_io(
        f"{_finecode_cmd} -m finecode.extension_runner.cli --trace --project-path={runner_info.working_dir_path.as_posix()}",
        runner_info.working_dir_path,
    )
    return runner_info


async def stop_extension_runner(runner: manager_api.ExtensionRunnerInfo) -> None:
    if runner.client is not None:
        logger.trace(f"Trying to stop extension runner {runner.working_dir_path}")
        # `runner.client.stop()` doesn't work, it just hangs. Need to be investigated. Terminate
        # forcefully until the problem is properly solved.
        runner.client._server.terminate()
        await runner.client.stop()
        logger.trace(f"Stop extension runner {runner.process_id} in {runner.working_dir_path}")
    else:
        logger.trace(
            f"Tried to stop extension runner {runner.working_dir_path}, but it was not running"
        )


def get_subactions(names: list[str], project_raw_config: dict[str, Any]) -> list[domain.Action]:
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


async def init_runner(
    runner: manager_api.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # initialization is required to be able to perform other requests
    logger.trace(f"Init runner {runner.working_dir_path}")
    assert runner.client is not None
    try:
        await asyncio.wait_for(
            runner.client.protocol.send_request_async(
                method=types.INITIALIZE,
                params=types.InitializeParams(
                    process_id=os.getpid(),
                    capabilities=types.ClientCapabilities(),
                    client_info=types.ClientInfo(
                        name="FineCode_WorkspaceManager", version="0.1.0"
                    ),
                    trace=types.TraceValue.Verbose,
                ),
            ),
            10,
        )
    except RuntimeError:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        runner.initialized_event.set()
        logger.error("Runner crashed?")
        stdout, stderr = await runner.client._server.communicate()

        logger.debug(f"[Runner exited with {runner.client._server.returncode}]")
        if stdout:
            logger.debug(f"[stdout]\n{stdout.decode()}")
        if stderr:
            logger.debug(f"[stderr]\n{stderr.decode()}")
        return
    except Exception as e:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        runner.initialized_event.set()
        logger.exception(e)
        return

    try:
        runner.client.protocol.notify(method=types.INITIALIZED, params=types.InitializedParams())
    except Exception as e:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        runner.initialized_event.set()
        logger.exception(e)
        return
    logger.debug("LSP Server initialized")

    await read_configs.read_project_config(project=project, ws_context=ws_context)
    collect_actions.collect_actions(project_path=project.dir_path, ws_context=ws_context)

    assert project.actions is not None, f"Actions of project {project.dir_path} are not read yet"
    all_actions = set([])
    actions_to_process = set(project.actions)
    while len(actions_to_process) > 0:
        action = actions_to_process.pop()
        all_actions.add(action)
        actions_to_process |= set(
            get_subactions(
                names=action.subactions,
                project_raw_config=ws_context.ws_projects_raw_configs[project.dir_path],
            )
        )
    all_actions_dict = {action.name: action for action in all_actions}

    try:
        # lsp client requests have no timeout, add own one
        try:
            await asyncio.wait_for(
                runner.client.protocol.send_request_async(
                    method=types.WORKSPACE_EXECUTE_COMMAND,
                    params=types.ExecuteCommandParams(
                        command="finecodeRunner/updateConfig",
                        arguments=[
                            runner.working_dir_path.as_posix(),
                            runner.working_dir_path.stem,
                            all_actions_dict,
                            project.actions_configs,
                        ],
                    ),
                ),
                10,
            )
        except TimeoutError:
            logger.error(f"Failed to update config of runner {runner.working_dir_path}")

        logger.debug(
            f"Updated config of runner {runner.working_dir_path}, process id {runner.process_id}"
        )
        project.status = domain.ProjectStatus.RUNNING
        runner.initialized_event.set()
    except Exception as e:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        runner.initialized_event.set()
        logger.exception(e)
