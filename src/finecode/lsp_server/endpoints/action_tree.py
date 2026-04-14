from __future__ import annotations

from typing import TYPE_CHECKING

from finecode.lsp_server import global_state
from loguru import logger

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


def _parse_parent_node_id(params) -> str | None:
    """Parse getActions executeCommand params (camelCase protocol)."""
    if params is None:
        return None

    if isinstance(params, str):
        return params

    if isinstance(params, dict):
        return params.get("parentNodeId")

    if isinstance(params, list):
        if len(params) == 0:
            return None
        first = params[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("parentNodeId")

    return None


def _parse_node_id(node_id: str) -> tuple[str, str]:
    """Split a node ID of the form ``project_path::action_source`` into its parts.

    Returns ``(project_path_str, action_source)``.
    Raises ``ValueError`` if the format is invalid.
    """
    parts = node_id.split("::", 1)
    if len(parts) < 2:
        raise ValueError(f"Invalid action node ID: {node_id!r}")
    return parts[0], parts[1]


async def notify_changed_action_node(ls: LspServer, action_node: dict) -> None:
    ls.notify_client("actionsNodes/changed", action_node)


async def list_actions(_ls: LspServer, params=None):
    logger.info(f"list_actions {params}")
    await global_state.server_initialized.wait()

    parent_node_id = _parse_parent_node_id(params)

    if global_state.wm_client is None:
        raise Exception()

    response = await global_state.wm_client.get_tree(parent_node_id)
    return response


async def list_actions_for_position(_ls: LspServer, params=None):
    logger.info(f"list_actions_for_position {params}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    response = await global_state.wm_client.get_tree(None)
    return response


async def run_action_on_file(ls: LspServer, params=None):
    logger.info(f"run action on file {params}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]
    project_path_str, action_source = _parse_node_id(action_node_id)

    document_meta = await ls.send_request_to_client(
        "editor/documentMeta", {}
    )
    if document_meta is None:
        return None

    run_params: dict = {"file_paths": [document_meta["uri"]], "target": "files"}
    # Format actions should not auto-save when triggered from the file context menu.
    if "format" in action_source.lower():
        run_params["save"] = False

    response = await global_state.wm_client.run_action(
        action_source=action_source,
        project=project_path_str,
        params=run_params,
        options={"trigger": "user", "devEnv": "ide"},
    )
    return response


async def run_action_on_project(_ls: LspServer, params=None):
    logger.info(f"run action on project {params}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]
    project_path_str, action_source = _parse_node_id(action_node_id)

    response = await global_state.wm_client.run_action(
        action_source=action_source,
        project=project_path_str,
        params={"target": "project"},
        options={"trigger": "user", "devEnv": "ide"},
    )
    return response


async def list_projects(_ls: LspServer):
    logger.info("list_projects")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    return await global_state.wm_client.list_projects()


async def run_batch(_ls: LspServer, params=None):
    logger.info(
        f"run_batch actions={params.get('actions')} options={params.get('options')}"
    )
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        logger.error("run_batch: wm_client is None")
        raise Exception("WM client not available")

    try:
        result = await global_state.wm_client.run_batch(
            action_sources=params["actions"],
            projects=params.get("projects"),
            params=params.get("params"),
            params_by_project=params.get("paramsByProject"),
            options=params.get("options", {"trigger": "user", "devEnv": "ide"}),
        )
        logger.info(
            f"run_batch done, projects={list(result.get('results', {}).keys())}"
        )
        return result
    except Exception:
        logger.exception("run_batch: WM request failed")
        raise


async def run_action(_ls: LspServer, params=None):
    logger.info(f"run_action {params}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    return await global_state.wm_client.run_action(
        action_source=params["action"],
        project=params["project"],
        params=params.get("params"),
        options=params.get("options", {"trigger": "user", "devEnv": "ide"}),
    )


async def reload_action(_ls: LspServer, params=None):
    logger.info(f"reload action {params}")
    await global_state.server_initialized.wait()

    if global_state.wm_client is None:
        raise Exception()

    params_dict = params[0]
    action_node_id = params_dict["projectPath"]

    await global_state.wm_client.request(
        "actions/reload", {"actionNodeId": action_node_id}
    )
    return {}
