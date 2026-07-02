from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from finecode.lsp_server import global_state, pygls_types_utils
from finecode.lsp_server.endpoints import _cancellation

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


def _range_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": {"line": r["start"]["line"], "character": r["start"]["character"]},
        "end": {"line": r["end"]["line"], "character": r["end"]["character"]},
    }


async def hover(_ls: LspServer, params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentHoverAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting hover for {uri}")
        logger.error(f"Error getting hover for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    content = json_result.get("content")
    if not content:
        return None
    result: dict[str, Any] = {"contents": {"kind": content["kind"], "value": content["value"]}}
    if json_result.get("range"):
        r = json_result["range"]
        result["range"] = _range_dict(r)
    return result


async def definition(_ls: LspServer, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentDefinitionAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting definition for {uri}")
        logger.error(f"Error getting definition for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    locations = json_result.get("locations") or []
    if not locations:
        return None
    return [{"uri": loc["uri"], "range": _range_dict(loc["range"])} for loc in locations]


async def references(_ls: LspServer, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    include_declaration: bool = (params.get("context") or {}).get("includeDeclaration", True)
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentReferencesAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
                "include_declaration": include_declaration,
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting references for {uri}")
        logger.error(f"Error getting references for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    locations = json_result.get("locations") or []
    if not locations:
        return None
    return [{"uri": loc["uri"], "range": _range_dict(loc["range"])} for loc in locations]


async def type_definition(_ls: LspServer, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentTypeDefinitionAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting type definition for {uri}")
        logger.error(f"Error getting type definition for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    locations = json_result.get("locations") or []
    if not locations:
        return None
    return [{"uri": loc["uri"], "range": _range_dict(loc["range"])} for loc in locations]


async def implementation(_ls: LspServer, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentImplementationAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting implementation for {uri}")
        logger.error(f"Error getting implementation for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    locations = json_result.get("locations") or []
    if not locations:
        return None
    return [{"uri": loc["uri"], "range": _range_dict(loc["range"])} for loc in locations]


async def document_highlight(_ls: LspServer, params: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if params is None or global_state.wm_client is None:
        return None
    uri: str = params["textDocument"]["uri"]
    position = params["position"]
    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None
    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_symbol_info.TextDocumentDocumentHighlightAction",
            project=project_dir,
            params={
                "uri": uri,
                "position": {"line": position["line"], "character": position["character"]},
            },
            options={"trigger": "user", "devEnv": "ide"},
        )
    except Exception as error:
        _cancellation.reraise_if_cancelled(error, context=f"Error getting document highlight for {uri}")
        logger.error(f"Error getting document highlight for {uri}: {error}")
        return None
    json_result = (response.get("resultByFormat") or {}).get("json") if response else None
    if not json_result:
        return None
    highlights = json_result.get("highlights") or []
    if not highlights:
        return None
    return [{"range": _range_dict(h["range"]), "kind": h["kind"]} for h in highlights]
