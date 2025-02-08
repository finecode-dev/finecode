import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from finecode.workspace_manager import context, domain, find_project
from finecode.workspace_manager.config import read_configs
from finecode.workspace_manager.runner import manager as runner_manager
from finecode.workspace_manager.runner import runner_client
from finecode.workspace_manager.server import (global_state, schemas,
                                               user_messages)


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


def register_send_user_message_notification_callback(send_user_message_notification_callback):
    user_messages._lsp_notification_send = send_user_message_notification_callback


def register_send_user_message_request_callback(send_user_message_request_callback):
    user_messages._lsp_message_send = send_user_message_request_callback


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


async def _dir_to_tree_node(
    dir_path: Path, ws_context: context.WorkspaceContext
) -> schemas.ActionTreeNode | None:
    # ignore directories: hidden directories like .git, .pytest_cache etc, node_modules
    if dir_path.name.startswith(".") or dir_path.name == "node_modules":
        return None

    # 1. Determine type of dir_path: project or directory
    dir_is_project = find_project.is_project(dir_path)
    dir_node_type = (
        schemas.ActionTreeNode.NodeType.PROJECT
        if dir_is_project
        else schemas.ActionTreeNode.NodeType.DIRECTORY
    )
    subnodes: list[schemas.ActionTreeNode] = []
    status = ""
    if dir_is_project:
        try:
            project = ws_context.ws_projects[dir_path]
        except KeyError:
            logger.trace(f"Project exists in {dir_path}, but no config found")
            project = None

        if project is not None:
            status = project.status.name
            if project.status == domain.ProjectStatus.RUNNING:
                assert project.actions is not None
                for action in project.actions:
                    if action.name not in project.root_actions:
                        continue

                    node_id = f"{project.dir_path.as_posix()}::{action.name}"
                    subnodes.append(
                        schemas.ActionTreeNode(
                            node_id=node_id,
                            name=action.name,
                            node_type=schemas.ActionTreeNode.NodeType.ACTION,
                            subnodes=[],
                            status="",
                        )
                    )
                    ws_context.cached_actions_by_id[node_id] = context.CachedAction(
                        action_id=node_id, project_path=project.dir_path, action_name=action.name
                    )
            # TODO: presets?
        else:
            logger.info(f"Project is not running: {project.dir_path}")
            ...  # TODO: error status
    else:
        for dir_item in dir_path.iterdir():
            if dir_item.is_dir():
                subnode = await _dir_to_tree_node(dir_item, ws_context)
                if subnode is not None:
                    subnodes.append(subnode)

    # TODO: cache result?
    return schemas.ActionTreeNode(
        node_id=dir_path.as_posix(),
        name=dir_path.name,
        subnodes=subnodes,
        node_type=dir_node_type,
        status=status,
    )


async def _list_actions(
    ws_context: context.WorkspaceContext, parent_node_id: str | None = None
) -> list[schemas.ActionTreeNode]:
    if parent_node_id is None:
        # list ws dirs and first level

        # wait for start of all runners, this is required to be able to resolve presets
        all_started_coros = [
            runner.initialized_event.wait()
            for runner in ws_context.ws_projects_extension_runners.values()
        ]
        await asyncio.gather(*all_started_coros)

        nodes: list[schemas.ActionTreeNode] = []
        for ws_dir_path in ws_context.ws_dirs_paths:
            node = await _dir_to_tree_node(ws_dir_path, ws_context)
            if node is not None:
                nodes.append(node)
        # sort nodes alphabetically to keep the order stable
        nodes.sort(key=lambda node: node.name)
        return nodes
    else:
        # TODO
        return []


async def list_actions(
    request: schemas.ListActionsRequest,
) -> schemas.ListActionsResponse:
    if len(global_state.ws_context.ws_dirs_paths) == 0:
        return schemas.ListActionsResponse(nodes=[])

    return schemas.ListActionsResponse(
        nodes=await _list_actions(
            global_state.ws_context,
            request.parent_node_id if request.parent_node_id != "" else None,
        )
    )


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: validate apply_on and apply_on_text

    _action_node_id = request.action_node_id
    if ":" not in _action_node_id:
        # general action without project path like 'format' or 'lint', normalize (=add project path)
        try:
            project_path = find_project.find_project_with_action_for_file(
                file_path=Path(request.apply_on),
                action_name=_action_node_id,
                ws_context=global_state.ws_context,
            )
        except ValueError:
            logger.warning(
                f"Skip {_action_node_id} on {request.apply_on}, because file is not in workspace"
            )
            return schemas.RunActionResponse({})
        _action_node_id = f"{project_path.as_posix()}::{_action_node_id}"

    splitted_action_id = _action_node_id.split("::")
    project_path = Path(splitted_action_id[0])
    try:
        project = global_state.ws_context.ws_projects[project_path]
    except KeyError:
        raise ActionNotFound()

    if project.actions is None:
        logger.error("Actions in project are not read yet, but expected")
        raise InternalError()

    action_name = splitted_action_id[1]
    try:
        action = next(action for action in project.actions if action.name == action_name)
    except (KeyError, StopIteration) as error:
        logger.error(f"Unexpected error, project or action not found: {error}")
        raise InternalError()

    logger.info("run action", request)
    result = await __run_action(
        action=action,
        apply_on=Path(request.apply_on) if request.apply_on != "" else None,
        apply_on_text=request.apply_on_text,
        project_root=project.dir_path,
        ws_context=global_state.ws_context,
    )
    # in case of error, there is no result in dict
    return schemas.RunActionResponse(result=result.get("result", {}))


async def __run_action(
    action: domain.Action,
    apply_on: Path | None,
    apply_on_text: str,
    project_root: Path,
    ws_context: context.WorkspaceContext,
) -> dict[str, Any]:
    logger.trace(f"Execute action {action.name} on {apply_on}")

    try:
        project_def = ws_context.ws_projects[project_root]
    except KeyError:
        logger.error(f"Project definition not found: {project_root}")
        return {}

    if project_def.status != domain.ProjectStatus.RUNNING:
        logger.error(
            f"Extension runner is not running in {project_def.dir_path}. Please check logs."
        )
        return {}

    try:
        next(a for a in project_def.actions if a.name == action.name)
    except StopIteration:
        action_found = False
        try:
            workspace_project = ws_context.ws_projects[project_root]
        except KeyError:
            logger.error(f"Workspace project not found: {project_root}")
            return {}

        if workspace_project.actions is None:
            logger.error("Actions in workspace project are not read yet")
            return {}

        try:
            next(a for a in workspace_project.actions if a.name == action.name)
            action_found = True
        except StopIteration:
            ...
        if not action_found:
            logger.error(f"Action {action.name} not found neither in project nor in workspace")
            return {}

    if apply_on is not None:
        ws_context.ignore_watch_paths.add(apply_on)

        # apply on file / apply on project: pass file as is and form list of files in case of project
        if apply_on.is_dir():
            all_apply_on = list(apply_on.rglob("*.py"))
        else:
            all_apply_on = [apply_on]
    else:
        all_apply_on = None

    if project_root in ws_context.ws_projects_extension_runners:
        # extension runner is running for this project, send command to it
        try:
            result = await runner_client.run_action(
                runner=ws_context.ws_projects_extension_runners[project_root],
                action=action,
                apply_on=all_apply_on,
                apply_on_text=apply_on_text,
            )
        except runner_client.BaseRunnerRequestException as error:
            error_message = error.args[0] if len(error.args) > 0 else ""
            await user_messages.error(f"Action {action.name} failed: {error_message}")
            return {}
    else:
        raise NotImplementedError()

    if apply_on is not None:
        try:
            ws_context.ignore_watch_paths.remove(apply_on)
        except KeyError:
            ...

    return result


async def reload_action(action_node_id: str) -> None:
    splitted_action_id = action_node_id.split("::")
    project_path = Path(splitted_action_id[0])
    try:
        project = global_state.ws_context.ws_projects[project_path]
    except KeyError:
        raise ActionNotFound()

    if project.actions is None:
        logger.error("Actions in project are not read yet, but expected")
        raise InternalError()

    action_name = splitted_action_id[1]
    try:
        next(action for action in project.actions if action.name == action_name)
    except StopIteration as error:
        logger.error(f"Unexpected error, project or action not found: {error}")
        raise InternalError()

    runner = global_state.ws_context.ws_projects_extension_runners[project_path]

    try:
        await runner_client.reload_action(runner, action_name)
    except runner_client.BaseRunnerRequestException as error:
        error_message = error.args[0] if len(error.args) > 0 else ""
        await user_messages.error(f"Action {action_name} reload failed: {error_message}")


async def handle_changed_ws_dirs(added: list[Path], removed: list[Path]) -> None:
    for added_ws_dir_path in added:
        global_state.ws_context.ws_dirs_paths.append(added_ws_dir_path)

    for removed_ws_dir_path in removed:
        try:
            global_state.ws_context.ws_dirs_paths.remove(removed_ws_dir_path)
        except ValueError:
            logger.warning(
                f"Ws Directory {removed_ws_dir_path} was removed from ws, but not found in ws context"
            )

    await runner_manager.update_runners(global_state.ws_context)


async def restart_extension_runner(runner_working_dir_path: Path) -> None:
    # TODO: reload config?
    try:
        runner = global_state.ws_context.ws_projects_extension_runners[runner_working_dir_path]
    except KeyError:
        logger.error(f"Cannot find runner for {runner_working_dir_path}")
        return

    # `stop_extension_runner` waits for end of the process, explicit shutdown request is required
    # to stop it
    # await runner.client.protocol.send_request_async(types.SHUTDOWN)
    await runner_manager.stop_extension_runner(runner)
    new_runner = await runner_manager.start_extension_runner(
        runner_dir=runner_working_dir_path, ws_context=global_state.ws_context
    )
    global_state.ws_context.ws_projects_extension_runners[runner_working_dir_path] = new_runner


async def on_shutdown():
    get_running_runners = lambda: [
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
        kill_coros = [runner_manager.kill_extension_runner(runner) for runner in running_runners]
        await asyncio.gather(*kill_coros)
        logger.info(f"Killed {len(running_runners)} running runners")
