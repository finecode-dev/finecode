from pathlib import Path

from loguru import logger

from finecode import domain, user_messages
from finecode.config import collect_actions, config_models, read_configs
from finecode.lsp_server import global_state, schemas
from finecode.runner import manager as runner_manager


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
    new_projects = await read_configs.read_projects_in_dir(
        dir_path, global_state.ws_context
    )

    for new_project in new_projects:
        await read_configs.read_project_config(
            project=new_project,
            ws_context=global_state.ws_context,
            resolve_presets=False,
        )

    try:
        await runner_manager.start_runners_with_presets(
            projects=new_projects, ws_context=global_state.ws_context
        )
    except runner_manager.RunnerFailedToStart as exception:
        raise ValueError(f"Starting runners with presets failed: {exception.message}")

    return schemas.AddWorkspaceDirResponse()


async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    ws_dir_path_to_remove = Path(request.dir_path)
    global_state.ws_context.ws_dirs_paths.remove(ws_dir_path_to_remove)

    # find all projects affected by removing of this ws dir
    project_dir_pathes = global_state.ws_context.ws_projects.keys()
    projects_to_remove: list[Path] = []
    for project_dir_path in project_dir_pathes:
        if not project_dir_path.is_relative_to(ws_dir_path_to_remove):
            continue

        # project_dir_path is now candidate to remove
        remove_project_dir_path = True
        for ws_dir_path in global_state.ws_context.ws_dirs_paths:
            if project_dir_path.is_relative_to(ws_dir_path):
                # project is also in another ws_dir, keep it
                remove_project_dir_path = False
                break

        if remove_project_dir_path:
            project_runners = global_state.ws_context.ws_projects_extension_runners[
                project_dir_path
            ].values()
            for runner in project_runners:
                await runner_manager.stop_extension_runner(runner=runner)
            del global_state.ws_context.ws_projects[project_dir_path]
            try:
                del global_state.ws_context.ws_projects_raw_configs[project_dir_path]
            except KeyError:
                ...

    return schemas.DeleteWorkspaceDirResponse()


async def handle_changed_ws_dirs(added: list[Path], removed: list[Path]) -> None:
    for removed_ws_dir_path in removed:
        delete_request = schemas.DeleteWorkspaceDirRequest(
            dir_path=removed_ws_dir_path.as_posix()
        )
        await delete_workspace_dir(request=delete_request)

    for added_ws_dir_path in added:
        add_request = schemas.AddWorkspaceDirRequest(
            dir_path=added_ws_dir_path.as_posix()
        )
        await add_workspace_dir(request=add_request)
