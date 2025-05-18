from pathlib import Path

from loguru import logger

from finecode.workspace_manager import domain, user_messages
from finecode.workspace_manager.config import read_configs
from finecode.workspace_manager.lsp_server import global_state, schemas
from finecode.workspace_manager.runner import manager as runner_manager


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
    user_messages._notification_sender = send_user_message_notification_callback


def register_send_user_message_request_callback(send_user_message_request_callback):
    user_messages._lsp_message_send = send_user_message_request_callback


def register_document_getter(get_document_func):
    runner_manager.get_document = get_document_func


def register_workspace_edit_applier(apply_workspace_edit_func):
    runner_manager.apply_workspace_edit = apply_workspace_edit_func


def register_progress_reporter(report_progress_func):
    global_state.progress_reporter = report_progress_func


async def add_workspace_dir(
    request: schemas.AddWorkspaceDirRequest,
) -> schemas.AddWorkspaceDirResponse:
    logger.trace(f"Add workspace dir {request.dir_path}")
    dir_path = Path(request.dir_path)

    if dir_path in global_state.ws_context.ws_dirs_paths:
        raise ValueError("Directory is already added")

    global_state.ws_context.ws_dirs_paths.append(dir_path)
    await read_configs.read_projects_in_dir(dir_path, global_state.ws_context)
    try:
        await runner_manager.update_runners(global_state.ws_context)
    except runner_manager.RunnerFailedToStart:
        # user sees status in client(IDE), no need to raise explicit error
        ...
    return schemas.AddWorkspaceDirResponse()


async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    global_state.ws_context.ws_dirs_paths.remove(Path(request.dir_path))
    try:
        await runner_manager.update_runners(global_state.ws_context)
    except runner_manager.RunnerFailedToStart:
        # user sees status in client(IDE), no need to raise explicit error
        ...
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

    try:
        await runner_manager.update_runners(global_state.ws_context)
    except runner_manager.RunnerFailedToStart:
        # user sees status in client(IDE), no need to raise explicit error
        ...
