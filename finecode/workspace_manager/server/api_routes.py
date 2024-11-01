from pathlib import Path

from loguru import logger
from modapp import APIRouter
from modapp.errors import NotFoundError, ServerError

import finecode.api as api
import finecode.domain as domain
import finecode.workspace_context as workspace_context
import finecode.workspace_manager.api as manager_api
import finecode.workspace_manager.find_package as find_package
import finecode.workspace_manager.main as manager_main
import finecode.workspace_manager.server.schemas as schemas

from .endpoints import finecode

router = APIRouter()
ws_context = workspace_context.WorkspaceContext([])


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.AddWorkspaceDir)
async def add_workspace_dir(
    request: schemas.AddWorkspaceDirRequest,
) -> schemas.AddWorkspaceDirResponse:
    dir_path = Path(request.dir_path)
    ws_context.ws_dirs_paths.append(dir_path)
    api.read_configs_in_dir(dir_path=dir_path, ws_context=ws_context)
    await manager_main.update_runners(ws_context)
    return schemas.AddWorkspaceDirResponse()


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.DeleteWorkspaceDir)
async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    ws_context.ws_dirs_paths.remove(Path(request.dir_path))
    await manager_main.update_runners(ws_context)
    return schemas.DeleteWorkspaceDirResponse()


def _dir_to_tree_node(
    dir_path: Path, ws_context: workspace_context.WorkspaceContext
) -> schemas.ActionTreeNode | None:
    # ignore directories: hidden directories like .git, .pytest_cache etc, node_modules
    if dir_path.name.startswith(".") or dir_path.name == "node_modules":
        return None

    # 1. Determine type of dir_path: package or directory
    dir_is_package = find_package.is_package(dir_path)
    dir_node_type = (
        schemas.ActionTreeNode.NodeType.PACKAGE
        if dir_is_package
        else schemas.ActionTreeNode.NodeType.DIRECTORY
    )
    subnodes: list[schemas.ActionTreeNode] = []
    if dir_is_package:
        # reading configs for ws dirs is not needed here, because it happens on adding directory
        # to workspace. Read only for nested dirs
        if dir_path not in ws_context.ws_dirs_paths:
            # `read_configs_in_dir` looks for packages, parses them recursively and normalizes. It's
            # not needed in this case and can be simplified
            api.read_configs_in_dir(dir_path, ws_context)
        try:
            package = ws_context.ws_packages[dir_path]
        except KeyError:
            logger.trace(f"Package exists in {dir_path}, but no config found")
            package = None

        if package is not None:
            if package.actions is None:
                api.collect_actions(package_path=package.path, ws_context=ws_context)
            assert package.actions is not None
            for action in package.actions:
                if action.name not in package.root_actions:
                    continue

                node_id = f"{package.path.as_posix()}::{action.name}"
                subnodes.append(
                    schemas.ActionTreeNode(
                        node_id=node_id,
                        name=action.name,
                        node_type=schemas.ActionTreeNode.NodeType.ACTION,
                        subnodes=[],
                    )
                )
                ws_context.cached_actions_by_id[node_id] = workspace_context.CachedAction(
                    action_id=node_id, package_path=package.path, action_name=action.name
                )
        # TODO: presets?
    else:
        for dir_item in dir_path.iterdir():
            if dir_item.is_dir():
                subnode = _dir_to_tree_node(dir_item, ws_context)
                if subnode is not None:
                    subnodes.append(subnode)

    # TODO: cache result?
    return schemas.ActionTreeNode(
        node_id=dir_path.as_posix(), name=dir_path.name, subnodes=subnodes, node_type=dir_node_type
    )


def _list_actions(
    ws_context: workspace_context.WorkspaceContext, parent_node_id: str | None = None
) -> list[schemas.ActionTreeNode]:
    if parent_node_id is None:
        # list ws dirs and first level
        nodes: list[schemas.ActionTreeNode] = []
        for ws_dir_path in ws_context.ws_dirs_paths:
            node = _dir_to_tree_node(ws_dir_path, ws_context)
            if node is not None:
                nodes.append(node)
        # sort nodes alphabetically to keep the order stable
        nodes.sort(key=lambda node: node.name)
        return nodes
    else:
        # TODO
        return []


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.ListActions)
async def list_actions(
    request: schemas.ListActionsRequest,
) -> schemas.ListActionsResponse:
    if len(ws_context.ws_dirs_paths) == 0:
        return schemas.ListActionsResponse(nodes=[])

    return schemas.ListActionsResponse(
        nodes=_list_actions(
            ws_context, request.parent_node_id if request.parent_node_id != "" else None
        )
    )


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.RunAction)
async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: validate apply_on and apply_on_text

    _action_node_id = request.action_node_id
    if ":" not in _action_node_id:
        # general action without package path like 'format' or 'lint', normalize (=add package path)
        package_path = find_package.find_package_with_action_for_file(
            file_path=Path(request.apply_on), action_name=_action_node_id, ws_context=ws_context
        )
        _action_node_id = f"{package_path.as_posix()}::{_action_node_id}"

    try:
        cached_action = ws_context.cached_actions_by_id[_action_node_id]
    except KeyError:
        raise NotFoundError()

    try:
        package = ws_context.ws_packages[cached_action.package_path]
        if package.actions is None:
            logger.error("Actions in package are not read yet, but expected")
            raise ServerError()
        action = next(
            action for action in package.actions if action.name == cached_action.action_name
        )
    except (KeyError, StopIteration) as error:
        logger.error(f"Unexpected error, package or action not found: {error}")
        raise ServerError()

    logger.info("run action", request)
    result = await __run_action(
        action=action,
        apply_on=Path(request.apply_on) if request.apply_on != "" else None,
        apply_on_text=request.apply_on_text,
        project_root=package.path,
        ws_context=ws_context,
    )
    return schemas.RunActionResponse(result_text=result or "")


async def __run_action(
    action: domain.Action,
    apply_on: Path | None,
    apply_on_text: str,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> str | None:
    logger.trace(f"Execute action {action.name} on {apply_on}")

    try:
        project_package = ws_context.ws_packages[project_root]
    except KeyError:
        logger.error(f"Project package not found: {project_root}")
        return

    if project_package.actions is None:
        logger.error("Project actions are not read yet")
        return

    try:
        next(a for a in project_package.actions if a.name == action.name)
    except StopIteration:
        action_found = False
        try:
            workspace_package = ws_context.ws_packages[project_root]
        except KeyError:
            logger.error(f"Workspace package not found: {project_root}")
            return

        if workspace_package.actions is None:
            logger.error("Actions in workspace package are not read yet")
            return

        try:
            next(a for a in workspace_package.actions if a.name == action.name)
            action_found = True
        except StopIteration:
            ...
        if not action_found:
            logger.error(f"Action {action.name} not found neither in project nor in workspace")
            return

    if apply_on:
        ws_context.ignore_watch_paths.add(apply_on)

    if project_root in ws_context.ws_packages_extension_runners:
        # extension runner is running for this project, send command to it
        result = await manager_api.run_action_in_runner(
            runner=ws_context.ws_packages_extension_runners[project_root],
            action=action,
            apply_on=apply_on,
            apply_on_text=apply_on_text,
        )
    else:
        raise NotImplementedError()
        # # no extension runner, use CLI
        # # TODO: check that project is managed via poetry
        # exit_code, output = run_utils.run_cmd_in_dir(
        #     f"poetry run python -m finecode.cli action run {action.name} {apply_on.absolute().as_posix()}",
        #     dir_path=project_root,
        # )
        # logger.debug(f"Output: {output}")
        # if exit_code != 0:
        #     logger.error(f"Action execution failed: {output}")
        # else:
        #     logger.success(f"Action {action.name} successfully executed")

        # result = '' # TODO: correct result

    if apply_on is not None:
        try:
            ws_context.ignore_watch_paths.remove(apply_on)
        except KeyError:
            ...

    return result
