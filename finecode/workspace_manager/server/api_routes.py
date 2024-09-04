from pathlib import Path
from loguru import logger
from modapp import APIRouter
from modapp.errors import NotFoundError, ServerError
from .endpoints import finecode
import finecode.workspace_manager.server.schemas as schemas
import finecode.api as api
import finecode.workspace_context as workspace_context

router = APIRouter()
ws_context = workspace_context.WorkspaceContext([])


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.AddWorkspaceDir)
async def add_workspace_dir(
    request: schemas.AddWorkspaceDirRequest,
) -> schemas.AddWorkspaceDirResponse:
    dir_path = Path(request.dir_path)
    ws_context.ws_dirs_paths.append(dir_path)
    api.read_configs_in_dir(dir_path=dir_path, ws_context=ws_context)
    ws_context.ws_dirs_paths_changed.set()
    return schemas.AddWorkspaceDirResponse()


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.DeleteWorkspaceDir)
async def delete_workspace_dir(
    request: schemas.DeleteWorkspaceDirRequest,
) -> schemas.DeleteWorkspaceDirResponse:
    ws_context.ws_dirs_paths.remove(Path(request.dir_path))
    ws_context.ws_dirs_paths_changed.set()
    return schemas.DeleteWorkspaceDirResponse()


def _dir_to_tree_node(dir_path: Path, ws_context: workspace_context.WorkspaceContext) -> schemas.ActionTreeNode:
    # 1. Determine type of dir_path: package or directory
    dir_is_package = api.is_package(dir_path)
    dir_node_type = schemas.ActionTreeNode.NodeType.PACKAGE if dir_is_package else schemas.ActionTreeNode.NodeType.DIRECTORY
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
                api.collect_actions.collect_actions(package_path=package.path, ws_context=ws_context)
            assert package.actions is not None
            for action in package.actions:
                node_id = f'{package.path.as_posix()}::{action.name}'
                subnodes.append(schemas.ActionTreeNode(node_id=node_id, name=action.name, node_type=schemas.ActionTreeNode.NodeType.ACTION, subnodes=[]))
                ws_context.cached_actions_by_id[node_id] = workspace_context.CachedAction(action_id=node_id, package_path=package.path, action_name=action.name)
        # TODO: presets
    else:
        for dir_item in dir_path.iterdir():
            if dir_item.is_dir():
                subnodes.append(_dir_to_tree_node(dir_item, ws_context))

    # TODO: cache result?
    return schemas.ActionTreeNode(node_id=dir_path.as_posix(), name=dir_path.name, subnodes=subnodes, node_type=dir_node_type)


def _list_actions(ws_context: workspace_context.WorkspaceContext, parent_node_id: str | None = None) -> list[schemas.ActionTreeNode]:
    if parent_node_id is None:
        # list ws dirs and first level
        nodes: list[schemas.ActionTreeNode] =[]
        for ws_dir_path in ws_context.ws_dirs_paths:
            nodes.append(_dir_to_tree_node(ws_dir_path, ws_context))
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

    return schemas.ListActionsResponse(nodes=_list_actions(ws_context, request.parent_node_id if request.parent_node_id != '' else None))


@router.endpoint(finecode.workspace_manager.WorkspaceManagerService.RunAction)
async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    try:
        cached_action = ws_context.cached_actions_by_id[request.action_node_id]
    except KeyError:
        raise NotFoundError()

    try:
        package = ws_context.ws_packages[cached_action.package_path]
        if package.actions is None:
            logger.error("Actions in package are not read yet, but expected")
            raise ServerError()
        action = next(action for action in package.actions if action.name == cached_action.action_name)
    except (KeyError, StopIteration) as error:
        logger.error(f"Unexpected error, package or action not found: {error}")
        raise ServerError()

    api.run_action.__run_action(action=action, apply_on=Path(request.apply_on), project_root=package.path, ws_context=ws_context)
    # TODO: response
    print('run action', request)
    return schemas.RunActionResponse()
