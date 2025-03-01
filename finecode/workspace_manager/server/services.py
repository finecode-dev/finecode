import asyncio
from pathlib import Path

from loguru import logger

from finecode.workspace_manager import domain
from finecode.workspace_manager.config import read_configs
from finecode.workspace_manager.runner import manager as runner_manager
from finecode.workspace_manager.server import global_state, schemas, user_messages


class ActionNotFound(Exception): ...


class InternalError(Exception): ...


def register_project_changed_callback(action_node_changed_callback):
    async def project_changed_callback(project: domain.Project) -> None:
        action_node = schemas.ActionTreeNode(
            node_id=project.dir_path.as_posix(),
            name=project.name,
            subnodes=[],
            node_type=schemas.ActionTreeNode.NodeType.PROJECT,
            status=project.status.name,
        )
        await action_node_changed_callback(action_node)

    runner_manager.project_changed_callback = project_changed_callback


def register_send_user_message_notification_callback(
    send_user_message_notification_callback,
):
    user_messages._lsp_notification_send = send_user_message_notification_callback


def register_send_user_message_request_callback(send_user_message_request_callback):
    user_messages._lsp_message_send = send_user_message_request_callback


def register_document_getter(get_document_func):
    runner_manager.get_document = get_document_func


def register_workspace_edit_applier(apply_workspace_edit_func):
    runner_manager.apply_workspace_edit = apply_workspace_edit_func


async def add_workspace_dir(
    request: schemas.AddWorkspaceDirRequest,
) -> schemas.AddWorkspaceDirResponse:
    logger.trace(f"Add workspace dir {request.dir_path}")
    dir_path = Path(request.dir_path)

    if dir_path in global_state.ws_context.ws_dirs_paths:
        raise ValueError("Directory is already added")

    global_state.ws_context.ws_dirs_paths.append(dir_path)
    await read_configs.read_projects_in_dir(dir_path, global_state.ws_context)
    await runner_manager.update_runners(global_state.ws_context)
    return schemas.AddWorkspaceDirResponse()


async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    global_state.ws_context.ws_dirs_paths.remove(Path(request.dir_path))
    await runner_manager.update_runners(global_state.ws_context)
    return schemas.DeleteWorkspaceDirResponse()


async def handle_changed_ws_dirs(added: list[Path], removed: list[Path]) -> None:
    for added_ws_dir_path in added:
        global_state.ws_context.ws_dirs_paths.append(added_ws_dir_path)

    for removed_ws_dir_path in removed:
        try:
            global_state.ws_context.ws_dirs_paths.remove(removed_ws_dir_path)
        except ValueError:
            logger.warning(
                f"Ws Directory {removed_ws_dir_path} was removed from ws,"
                " but not found in ws context"
            )

    await runner_manager.update_runners(global_state.ws_context)


async def restart_extension_runner(runner_working_dir_path: Path) -> None:
    # TODO: reload config?
    try:
        runner = global_state.ws_context.ws_projects_extension_runners[
            runner_working_dir_path
        ]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    # `stop_extension_runner` waits for end of the process, explicit shutdown request
    # is required to stop it
    # await runner.client.protocol.send_request_async(types.SHUTDOWN)
    await runner_manager.stop_extension_runner(runner)
    new_runner = await runner_manager.start_extension_runner(
        runner_dir=runner_working_dir_path, ws_context=global_state.ws_context
    )
    global_state.ws_context.ws_projects_extension_runners[runner_working_dir_path] = (
        new_runner
    )


async def on_shutdown():
    def get_running_runners():
        return [
            runner
            for runner in global_state.ws_context.ws_projects_extension_runners.values()
            if global_state.ws_context.ws_projects[runner.working_dir_path].status
            == domain.ProjectStatus.RUNNING
        ]
    logger.info("Check that all runners stop in 5 seconds")
    seconds_waited = 0
    running_runners = get_running_runners()

    while seconds_waited < 5:
        await asyncio.sleep(1)
        seconds_waited += 1
        running_runners = get_running_runners()
        if len(running_runners) == 0:
            break

    if len(running_runners) > 0:
        logger.debug("Not all runners stopped after 5 seconds, kill running")
        kill_coros = [
            runner_manager.kill_extension_runner(runner) for runner in running_runners
        ]
        await asyncio.gather(*kill_coros)
        logger.info(f"Killed {len(running_runners)} running runners")
