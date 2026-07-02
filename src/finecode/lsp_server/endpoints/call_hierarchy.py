from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from finecode.lsp_server import global_state, pygls_types_utils
from finecode.lsp_server.endpoints import _cancellation

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _range_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": {"line": r["start"]["line"], "character": r["start"]["character"]},
        "end": {"line": r["end"]["line"], "character": r["end"]["character"]},
    }


def _lsp_item_to_action(lsp_item: dict[str, Any]) -> dict[str, Any]:
    """Convert an LSP CallHierarchyItem (camelCase) to action payload form (snake_case)."""
    return {
        "name": lsp_item["name"],
        "kind": lsp_item["kind"],
        "uri": lsp_item["uri"],
        "range": _range_to_dict(lsp_item["range"]),
        "selection_range": _range_to_dict(lsp_item["selectionRange"]),
        "detail": lsp_item.get("detail"),
        "tags": lsp_item.get("tags"),
    }


def _action_item_to_lsp(item: dict[str, Any]) -> dict[str, Any]:
    """Convert an action-result CallHierarchyItem (snake_case) to LSP wire form (camelCase)."""
    result: dict[str, Any] = {
        "name": item["name"],
        "kind": item["kind"],
        "uri": item["uri"],
        "range": _range_to_dict(item["range"]),
        "selectionRange": _range_to_dict(item["selection_range"]),
    }
    if item.get("detail") is not None:
        result["detail"] = item["detail"]
    if item.get("tags") is not None:
        result["tags"] = item["tags"]
    return result


# ---------------------------------------------------------------------------
# Endpoint handlers
# ---------------------------------------------------------------------------


async def prepare_call_hierarchy(
    _ls: LspServer, params: dict | None
) -> list[dict] | None:
    if params is None:
        return None
    if global_state.wm_client is None:
        return None

    uri: str = params["textDocument"]["uri"]
    position = params["position"]

    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_code_hierarchy.TextDocumentPrepareCallHierarchyAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "system", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error preparing call hierarchy for {uri}")
        logger.error(f"Error preparing call hierarchy for {uri}: {error}")
        return None

    if response is None:
        return None
    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None

    items = json_result.get("items") or []
    if not items:
        return None
    return [_action_item_to_lsp(item) for item in items]


async def call_hierarchy_incoming_calls(
    _ls: LspServer, params: dict | None
) -> list[dict] | None:
    if params is None:
        return None
    if global_state.wm_client is None:
        return None

    lsp_item = params["item"]
    uri: str = lsp_item["uri"]

    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_code_hierarchy.CallHierarchyIncomingCallsAction",
            project=project_dir,
            params={"item": _lsp_item_to_action(lsp_item)},
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting incoming calls for {uri}")
        logger.error(f"Error getting incoming calls for {uri}: {error}")
        return None

    if response is None:
        return None
    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None

    calls = json_result.get("calls") or []
    if not calls:
        return None
    return [
        {
            "from": _action_item_to_lsp(call["caller"]),
            "fromRanges": [_range_to_dict(r) for r in call["call_ranges"]],
        }
        for call in calls
    ]


async def call_hierarchy_outgoing_calls(
    _ls: LspServer, params: dict | None
) -> list[dict] | None:
    if params is None:
        return None
    if global_state.wm_client is None:
        return None

    lsp_item = params["item"]
    uri: str = lsp_item["uri"]

    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_code_hierarchy.CallHierarchyOutgoingCallsAction",
            project=project_dir,
            params={"item": _lsp_item_to_action(lsp_item)},
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting outgoing calls for {uri}")
        logger.error(f"Error getting outgoing calls for {uri}: {error}")
        return None

    if response is None:
        return None
    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None

    calls = json_result.get("calls") or []
    if not calls:
        return None
    return [
        {
            "to": _action_item_to_lsp(call["callee"]),
            "fromRanges": [_range_to_dict(r) for r in call["call_ranges"]],
        }
        for call in calls
    ]
