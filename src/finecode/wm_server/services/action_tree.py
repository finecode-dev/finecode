"""Action tree

This module contains the logic that constructs the hierarchical action tree used by the
IDE. It also provides the request handler that the WM server exposes
as ``actions/getTree``.
"""

from __future__ import annotations

import asyncio
import pathlib
from loguru import logger

from finecode.wm_server import context, domain


def _project_action_tree(project: domain.Project | None, ws_context: context.WorkspaceContext) -> list[dict]:
    """Return action/env nodes for a single project.

    ``project`` may be None when constructing a node for a directory without a
    project at its root.

    Side effect: populate ``ws_context.cached_actions_by_id`` so that later
    ``actions/run`` requests can resolve action node identifiers.
    """
    actions_nodes: list[dict] = []
    if project is None:
        return actions_nodes

    if project.status == domain.ProjectStatus.CONFIG_VALID:
        assert project.actions is not None

        action_nodes: list[dict] = []
        for action in project.actions:
            node_id = f"{project.dir_path.as_posix()}::{action.name}"
            handlers_nodes: list[dict] = []
            for handler in action.handlers:
                handler_node_id = f"{project.dir_path.as_posix()}::{action.name}::{handler.name}"
                handlers_nodes.append(
                    {
                        "node_id": handler_node_id,
                        "name": handler.name,
                        "node_type": 2,  # ACTION
                        "subnodes": [],
                        "status": "",
                    }
                )
            action_nodes.append(
                {
                    "node_id": node_id,
                    "name": action.name,
                    "node_type": 2,  # ACTION
                    "subnodes": handlers_nodes,
                    "status": "",
                }
            )
            ws_context.cached_actions_by_id[node_id] = context.CachedAction(
                action_id=node_id,
                project_path=project.dir_path,
                action_name=action.name,
            )

        node_id = f"{project.dir_path.as_posix()}::actions"
        actions_nodes.append(
            {
                "node_id": node_id,
                "name": "Actions",
                "node_type": 3,  # ACTION_GROUP
                "subnodes": action_nodes,
                "status": "",
            }
        )

        envs_nodes: list[dict] = []
        for env in project.envs:
            env_node_id = f"{project.dir_path.as_posix()}::envs::{env}"
            envs_nodes.append(
                {
                    "node_id": env_node_id,
                    "name": env,
                    "node_type": 6,  # ENV
                    "subnodes": [],
                    "status": "",
                }
            )
        node_id = f"{project.dir_path.as_posix()}::envs"
        actions_nodes.append(
            {
                "node_id": node_id,
                "name": "Environments",
                "node_type": 5,  # ENV_GROUP
                "subnodes": envs_nodes,
                "status": "",
            }
        )
    else:
        logger.info(
            f"Project has no valid config and finecode: {project.dir_path}, no actions will be shown"
        )

    return actions_nodes


def _build_tree(ws_context: context.WorkspaceContext) -> list[dict]:
    """Construct full workspace action tree as list of node dictionaries."""
    nodes: list[dict] = []
    projects_by_ws_dir: dict[pathlib.Path, list[pathlib.Path]] = {}

    all_ws_dirs = list(ws_context.ws_dirs_paths)
    all_ws_dirs.sort()

    all_projects_paths = list(ws_context.ws_projects.keys())
    all_projects_paths.sort()
    all_projects_paths_set = set(all_projects_paths)

    for ws_dir in all_ws_dirs:
        ws_dir_projects = [p for p in all_projects_paths_set if p.is_relative_to(ws_dir)]
        projects_by_ws_dir[ws_dir] = ws_dir_projects
        all_projects_paths_set -= set(ws_dir_projects)

    if all_projects_paths_set:
        logger.warning(
            f"Unexpected setup: these projects {all_projects_paths_set} don't belong to any of workspace dirs: {all_ws_dirs}"
        )

    for ws_dir in ws_context.ws_dirs_paths:
        ws_dir_projects = projects_by_ws_dir.get(ws_dir, [])
        ws_dir_nodes_by_path: dict[pathlib.Path, dict] = {}

        if ws_dir in ws_dir_projects:
            dir_node_type = 1  # PROJECT
            project = ws_context.ws_projects.get(ws_dir)
            status = project.status.name if project is not None else ""
        else:
            dir_node_type = 0  # DIRECTORY
            status = ""

        actions_nodes = _project_action_tree(ws_context.ws_projects.get(ws_dir), ws_context)
        node = {
            "node_id": ws_dir.as_posix(),
            "name": ws_dir.name,
            "subnodes": actions_nodes,
            "node_type": dir_node_type,
            "status": status,
        }
        nodes.append(node)
        ws_dir_nodes_by_path[ws_dir] = node

        for project_path in ws_dir_projects:
            project = ws_context.ws_projects.get(project_path)
            status = project.status.name if project is not None else ""
            actions_nodes = _project_action_tree(project, ws_context)
            node = {
                "node_id": project_path.as_posix(),
                "name": project_path.name,
                "subnodes": actions_nodes,
                "node_type": 1,  # PROJECT
                "status": status,
            }

            for ws_dir_node_path in reversed(list(ws_dir_nodes_by_path.keys())):
                if project_path.is_relative_to(ws_dir_node_path):
                    ws_dir_nodes_by_path[ws_dir_node_path]["subnodes"].append(node)
                    break

            ws_dir_nodes_by_path[project_path] = node

    return nodes


async def _handle_get_tree(
    params: dict | None, ws_context: context.WorkspaceContext
) -> dict:
    """Request handler that returns the action tree for the workspace."""

    # wait for dev_workspace runners to start
    async with asyncio.TaskGroup() as tg:
        for envs in ws_context.ws_projects_extension_runners.values():
            dev_workspace_runner = envs.get("dev_workspace")
            if dev_workspace_runner is not None:
                tg.create_task(dev_workspace_runner.initialized_event.wait())

    nodes = _build_tree(ws_context)
    return {"nodes": nodes}
