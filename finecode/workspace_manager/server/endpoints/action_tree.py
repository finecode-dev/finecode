import asyncio
from pathlib import Path
from typing import Any

from pygls.lsp.server import LanguageServer
from loguru import logger
from finecode.workspace_manager import context, domain, project_analyzer
from finecode.workspace_manager.runner import runner_client
from finecode.workspace_manager.server import global_state, schemas, user_messages
from finecode.workspace_manager.server.services import ActionNotFound, InternalError


async def list_actions(ls: LanguageServer, params):
    logger.info(f"list_actions {params}")
    await global_state.server_initialized.wait()

    parent_node_id = params[0]
    request = schemas.ListActionsRequest(parent_node_id=parent_node_id)
    result = await _list_actions(request=request)
    return result.model_dump(by_alias=True)


async def list_actions_for_position(ls: LanguageServer, params):
    logger.info(f"list_actions for position {params}")
    await global_state.server_initialized.wait()

    # position = params[0]
    # TODO
    request = schemas.ListActionsRequest(parent_node_id="")
    result = await _list_actions(request=request)
    return result.model_dump(by_alias=True)


def get_project_action_tree(
    project: domain.Project, ws_context: context.WorkspaceContext
) -> list[schemas.ActionTreeNode]:
    actions_nodes: list[schemas.ActionTreeNode] = []
    if project.status == domain.ProjectStatus.RUNNING:
        assert project.actions is not None
        for action in project.actions:
            if action.name not in project.root_actions:
                continue

            node_id = f"{project.dir_path.as_posix()}::{action.name}"
            subactions_nodes = [
                schemas.ActionTreeNode(
                    node_id=f"{project.dir_path.as_posix()}::{subaction_name}",
                    name=subaction_name,
                    node_type=schemas.ActionTreeNode.NodeType.ACTION,
                    subnodes=[],
                    status="",
                )
                for subaction_name in action.subactions
            ]
            actions_nodes.append(
                schemas.ActionTreeNode(
                    node_id=node_id,
                    name=action.name,
                    node_type=schemas.ActionTreeNode.NodeType.ACTION,
                    subnodes=subactions_nodes,
                    status="",
                )
            )
            ws_context.cached_actions_by_id[node_id] = context.CachedAction(
                action_id=node_id,
                project_path=project.dir_path,
                action_name=action.name,
            )
    else:
        logger.info(
            f"Project is not running: {project.dir_path}, no actions will be shown"
        )

    return actions_nodes


def create_node_list_for_ws(
    ws_context: context.WorkspaceContext,
) -> list[schemas.ActionTreeNode]:
    nodes: list[schemas.ActionTreeNode] = []
    projects_by_ws_dir: dict[Path, list[Path]] = {}

    all_ws_dirs = list(ws_context.ws_dirs_paths)
    all_ws_dirs.sort()

    all_projects_paths = list(ws_context.ws_projects.keys())
    all_projects_paths.sort()

    while len(all_ws_dirs) > 0:
        ws_dir = all_ws_dirs.pop()
        projects_by_ws_dir[ws_dir] = []

        while True:
            project_path = all_projects_paths[0]
            if project_path.is_relative_to(ws_dir):
                projects_by_ws_dir[ws_dir].append(project_path)
                all_projects_paths.pop(0)

            if len(all_projects_paths) == 0:
                break

    # build node tree so that:
    # - all ws dirs are in tree either as project or directory
    # - all projects are shown with subprojects and actions and subactions
    for ws_dir in ws_context.ws_dirs_paths:
        ws_dir_projects = projects_by_ws_dir[ws_dir]
        ws_dir_nodes_by_path: dict[Path, schemas.ActionTreeNode] = {}

        # process ws_dir separately, because only it can be directory
        if ws_dir in ws_dir_projects:
            dir_node_type = schemas.ActionTreeNode.NodeType.PROJECT
            try:
                project = ws_context.ws_projects[ws_dir]
            except KeyError:
                logger.trace(f"Project exists in {ws_dir}, but no config found")
                project = None

            if project is not None:
                status = project.status.name
            else:
                status = ""
        else:
            dir_node_type = schemas.ActionTreeNode.NodeType.DIRECTORY
            status = ""

        actions_nodes = get_project_action_tree(project=project, ws_context=ws_context)
        node = schemas.ActionTreeNode(
            node_id=ws_dir.as_posix(),
            name=ws_dir.name,
            subnodes=actions_nodes,
            node_type=dir_node_type,
            status=status,
        )
        nodes.append(node)
        ws_dir_nodes_by_path[ws_dir] = node

        for project_path in ws_dir_projects:
            try:
                project = ws_context.ws_projects[project_path]
            except KeyError:
                logger.trace(f"Project exists in {project_path}, but no config found")
                project = None

            status = ""
            if project is not None:
                status = project.status.name

            actions_nodes = get_project_action_tree(
                project=project, ws_context=ws_context
            )
            node = schemas.ActionTreeNode(
                node_id=project_path.as_posix(),
                name=project_path.name,
                subnodes=actions_nodes,
                node_type=schemas.ActionTreeNode.NodeType.PROJECT,
                status=status,
            )

            # check from back(=from the deepest node) to find the nearest parent node
            for ws_dir_node_path in list(ws_dir_nodes_by_path.keys())[::-1]:
                if project_path.is_relative_to(ws_dir_node_path):
                    ws_dir_nodes_by_path[ws_dir_node_path].subnodes.append(node)
                    break

            ws_dir_nodes_by_path[project_path] = node

    return nodes


async def __list_actions(
    ws_context: context.WorkspaceContext, parent_node_id: str | None = None
) -> list[schemas.ActionTreeNode]:
    # currently it always returns full tree
    #
    # if parent_node_id is None:
    # list ws dirs and first level

    # wait for start of all runners, this is required to be able to resolve presets
    all_started_coros = [
        runner.initialized_event.wait()
        for runner in ws_context.ws_projects_extension_runners.values()
    ]
    await asyncio.gather(*all_started_coros)

    nodes: list[schemas.ActionTreeNode] = create_node_list_for_ws(ws_context)
    return nodes
    # else:
    #     # TODO
    #     return []


async def _list_actions(
    request: schemas.ListActionsRequest,
) -> schemas.ListActionsResponse:
    if len(global_state.ws_context.ws_dirs_paths) == 0:
        return schemas.ListActionsResponse(nodes=[])

    return schemas.ListActionsResponse(
        nodes=await __list_actions(
            global_state.ws_context,
            request.parent_node_id if request.parent_node_id != "" else None,
        )
    )


async def run_action_on_file(ls: LanguageServer, params):
    logger.info(f"run action on file {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]

    document_meta = await ls.protocol.send_request_async(
        method="editor/documentMeta", params={}, msg_id=None
    )
    if document_meta is None:
        return None

    action_node_id_parts = action_node_id.split("::")
    project_dir_path_str = action_node_id_parts[0]
    project_dir_path = Path(project_dir_path_str)
    action_name = action_node_id_parts[1]
    if action_name.endswith("_many"):
        file_paths_by_projects = project_analyzer.get_files_by_projects(
            projects_dirs_paths=[project_dir_path]
        )
        file_paths = file_paths_by_projects[project_dir_path]
        params = {"file_paths": file_paths}
        if action_name == "format_many":
            params["save"] = True
    else:
        params = {"file_path": document_meta.uri.path}
        if action_name == "format":
            params["save"] = False

    run_action_request = schemas.RunActionRequest(
        action_node_id=action_node_id,
        params=params,
    )
    response = await run_action(run_action_request)
    logger.debug(f"Response: {response}")

    return response.model_dump(by_alias=True)


async def run_action_on_project(ls: LanguageServer, params):
    logger.info(f"run action on project {params}")
    await global_state.server_initialized.wait()

    return {}
    # params_dict = params[0]
    # action_node_id = params_dict["projectPath"]
    # apply_on = action_node_id.split("::")[0]
    # run_action_request = schemas.RunActionRequest(
    #     action_node_id=action_node_id, apply_on=apply_on, apply_on_text=""
    # )
    # response = await services.run_action(run_action_request)
    # return response.model_dump(by_alias=True)


async def reload_action(ls: LanguageServer, params):
    logger.info(f"reload action {params}")
    await global_state.server_initialized.wait()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]
    await __reload_action(action_node_id)

    return {}


async def __reload_action(action_node_id: str) -> None:
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
        await user_messages.error(
            f"Action {action_name} reload failed: {error_message}"
        )


async def run_action(
    request: schemas.RunActionRequest,
) -> schemas.RunActionResponse:
    # TODO: validate apply_on and apply_on_text
    _action_node_id = request.action_node_id
    # if ":" not in _action_node_id:
    #     # general action without project path like 'format' or 'lint', normalize (=add project path)
    #     try:
    #         project_path = find_project.find_project_with_action_for_file(
    #             file_path=Path(request.apply_on),
    #             action_name=_action_node_id,
    #             ws_context=global_state.ws_context,
    #         )
    #     except ValueError:
    #         logger.warning(
    #             f"Skip {_action_node_id} on {request.apply_on}, because file is not in workspace"
    #         )
    #         return schemas.RunActionResponse({})
    #     _action_node_id = f"{project_path.as_posix()}::{_action_node_id}"

    splitted_action_id = _action_node_id.split("::")
    project_path = Path(splitted_action_id[0])
    try:
        project_def = global_state.ws_context.ws_projects[project_path]
    except KeyError:
        raise ActionNotFound()

    if project_def.actions is None:
        logger.error("Actions in project are not read yet, but expected")
        raise InternalError()

    action_name = splitted_action_id[1]
    try:
        action = next(
            action for action in project_def.actions if action.name == action_name
        )
    except (KeyError, StopIteration) as error:
        logger.error(f"Unexpected error, project or action not found: {error}")
        raise InternalError()

    logger.info("run action", request)
    result = await __run_action(
        action=action,
        params=request.params,
        project_def=project_def,
        ws_context=global_state.ws_context,
    )
    return schemas.RunActionResponse(result=result)


async def __run_action(
    action: domain.Action,
    params: dict[str, Any],
    project_def: domain.Project,
    ws_context: context.WorkspaceContext,
) -> dict[str, Any]:
    formatted_params = str(params)
    if len(formatted_params) > 100:
        formatted_params = f"{formatted_params[:100]}..."
    logger.trace(f"Execute action {action.name} with {formatted_params}")

    if project_def.status != domain.ProjectStatus.RUNNING:
        logger.error(
            f"Extension runner is not running in {project_def.dir_path}. Please check logs."
        )
        return {}

    # extension runner is running for this project, send command to it
    try:
        result = await runner_client.run_action(
            runner=ws_context.ws_projects_extension_runners[project_def.dir_path],
            action_name=action.name,
            params=[params],
        )
    except runner_client.BaseRunnerRequestException as error:
        error_message = error.args[0] if len(error.args) > 0 else ""
        await user_messages.error(f"Action {action.name} failed: {error_message}")
        return {}

    return result


async def notify_changed_action_node(
    ls: LanguageServer, action: schemas.ActionTreeNode
) -> None:
    ls.protocol.notify(method="actionsNodes/changed", params=action.model_dump(by_alias=True))
