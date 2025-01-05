import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

import finecode.workspace_manager.context as context
import finecode.workspace_manager.domain as domain
import finecode.workspace_manager.find_project as find_project
import finecode.workspace_manager.main as manager_main
import finecode.workspace_manager.config.read_configs as read_configs
import finecode.workspace_manager.runner.runner_client as runner_client
import finecode.workspace_manager.server.global_state as global_state
import finecode.workspace_manager.server.schemas as schemas
import finecode.workspace_manager.runner.manager as runner_manager


class ActionNotFound(Exception): ...


class InternalError(Exception): ...


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
    if dir_is_project:
        try:
            project = ws_context.ws_projects[dir_path]
        except KeyError:
            logger.trace(f"Project exists in {dir_path}, but no config found")
            project = None

        if project is not None:
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
                        )
                    )
                    ws_context.cached_actions_by_id[node_id] = context.CachedAction(
                        action_id=node_id, project_path=project.dir_path, action_name=action.name
                    )
            # TODO: presets?
        else:
            logger.info(f"Project is not running: {project.dir_path}")
            ... # TODO: error status
    else:
        for dir_item in dir_path.iterdir():
            if dir_item.is_dir():
                subnode = await _dir_to_tree_node(dir_item, ws_context)
                if subnode is not None:
                    subnodes.append(subnode)

    # TODO: cache result?
    return schemas.ActionTreeNode(
        node_id=dir_path.as_posix(), name=dir_path.name, subnodes=subnodes, node_type=dir_node_type
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
        project_path = find_project.find_project_with_action_for_file(
            file_path=Path(request.apply_on),
            action_name=_action_node_id,
            ws_context=global_state.ws_context,
        )
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
    return schemas.RunActionResponse(result=result["result"])


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
        logger.error(f"Extension runner is not running in {project_def.dir_path}. Please check logs.")
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
        result = await runner_client.run_action(
            runner=ws_context.ws_projects_extension_runners[project_root],
            action=action,
            apply_on=all_apply_on,
            apply_on_text=apply_on_text,
        )
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

    await runner_client.reload_action(runner, action_name)


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

    await manager_main.update_runners(global_state.ws_context)


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
