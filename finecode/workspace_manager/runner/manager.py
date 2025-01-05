import asyncio
import os
from pathlib import Path

import janus
from loguru import logger

import finecode.workspace_manager.config.raw_config_utils as raw_config_utils
import finecode.workspace_manager.context as context
import finecode.workspace_manager.domain as domain
import finecode.workspace_manager.finecode_cmd as finecode_cmd
import finecode.workspace_manager.runner.runner_client as runner_client
from finecode.workspace_manager.runner import runner_info
from finecode import dirs_utils
from finecode.pygls_client_utils import create_lsp_client_io
from finecode.workspace_manager.config import read_configs, collect_actions


async def start_extension_runner(
    runner_dir: Path, ws_context: context.WorkspaceContext
) -> runner_info.ExtensionRunnerInfo | None:
    runner_info_instance = runner_info.ExtensionRunnerInfo(
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

    runner_info_instance.client = await create_lsp_client_io(
        f"{_finecode_cmd} -m finecode.extension_runner.cli --trace --project-path={runner_info_instance.working_dir_path.as_posix()}",
        runner_info_instance.working_dir_path,
    )
    return runner_info_instance


async def stop_extension_runner(runner: runner_info.ExtensionRunnerInfo) -> None:
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


async def update_runners(ws_context: context.WorkspaceContext) -> None:
    extension_runners = list(ws_context.ws_projects_extension_runners.values())
    new_dirs, deleted_dirs = dirs_utils.find_changed_dirs(
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
        _init_runner(runner, ws_context.ws_projects[runner.working_dir_path], ws_context)
        for runner in extension_runners
    ]
    await asyncio.gather(*init_runners_coros)


async def _init_runner(
    runner: runner_info.ExtensionRunnerInfo,
    project: domain.Project,
    ws_context: context.WorkspaceContext,
) -> None:
    # initialization is required to be able to perform other requests
    logger.trace(f"Init runner {runner.working_dir_path}")
    assert runner.client is not None
    try:
        await runner_client.initialize(
            runner,
            client_process_id=os.getpid(),
            client_name="FineCode_WorkspaceManager",
            client_version="0.1.0",
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
        await runner_client.notify_initialized(runner)
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
            raw_config_utils.get_subactions(
                names=action.subactions,
                project_raw_config=ws_context.ws_projects_raw_configs[project.dir_path],
            )
        )
    all_actions_dict = {action.name: action for action in all_actions}

    try:
        runner_client.update_config(runner, all_actions_dict, project.actions_configs)
    except Exception as e:
        project.status = domain.ProjectStatus.RUNNER_FAILED
        runner.initialized_event.set()
        logger.exception(e)
        return

    logger.debug(
        f"Updated config of runner {runner.working_dir_path}, process id {runner.process_id}"
    )
    project.status = domain.ProjectStatus.RUNNING
    runner.initialized_event.set()
