from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from finecode.lsp_server import global_state, pygls_types_utils
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SEMANTIC_TOKEN_TYPES,
    SEMANTIC_TOKEN_MODIFIERS,
    SemanticToken,
)

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


def encode_semantic_tokens(tokens: list[SemanticToken]) -> list[int]:
    """Sort tokens and produce the LSP delta-encoded integer array.

    Each token is encoded as 5 integers:
      [deltaLine, deltaStartChar, length, tokenType, tokenModifiers]
    where deltaLine and deltaStartChar are relative to the previous token.
    The first token's deltas are relative to (0, 0).
    """
    if not tokens:
        return []

    sorted_tokens = sorted(tokens, key=lambda t: (t.line, t.char))

    result: list[int] = []
    prev_line = 0
    prev_char = 0

    for token in sorted_tokens:
        delta_line = token.line - prev_line
        # When on a new line, deltaStartChar is absolute from line start.
        # When on the same line, it is relative to the previous token.
        delta_char = token.char if delta_line > 0 else token.char - prev_char
        result.extend([
            delta_line,
            delta_char,
            token.length,
            token.token_type_index,
            token.token_modifiers_bitmask,
        ])
        prev_line = token.line
        prev_char = token.char

    return result


def _range_to_dict(range_: Any) -> dict[str, Any] | None:
    if range_ is None:
        return None
    return {
        "start": {"line": range_.start.line, "character": range_.start.character},
        "end": {"line": range_.end.line, "character": range_.end.character},
    }


async def _run_full_or_range(
    uri: str, range_dict: dict[str, Any] | None
) -> list[int] | None:
    if global_state.wm_client is None:
        return None

    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None

    params: dict[str, Any] = {"uri": uri}
    if range_dict is not None:
        params["range"] = range_dict

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_semantic_tokens.TextDocumentSemanticTokensAction",
            project=project_dir,
            params=params,
            options={"trigger": "system", "devEnv": "ide"},
        )
    except Exception as error:
        logger.error(f"Error getting semantic tokens for {uri}: {error}")
        return None

    if response is None:
        return None

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None

    raw_tokens = json_result.get("tokens") or []
    tokens = [
        SemanticToken(
            line=t["line"],
            char=t["char"],
            length=t["length"],
            token_type_index=t["token_type_index"],
            token_modifiers_bitmask=t["token_modifiers_bitmask"],
        )
        for t in raw_tokens
    ]
    return encode_semantic_tokens(tokens)


async def document_semantic_tokens_full(
    _ls: LspServer, params: dict | None
) -> dict | None:
    if params is None:
        return None
    uri = params["textDocument"]["uri"]
    data = await _run_full_or_range(uri, range_dict=None)
    if data is None:
        return None
    return {"data": data}


async def document_semantic_tokens_range(
    _ls: LspServer, params: dict | None
) -> dict | None:
    if params is None:
        return None
    uri = params["textDocument"]["uri"]
    range_dict = params.get("range")
    data = await _run_full_or_range(uri, range_dict=range_dict)
    if data is None:
        return None
    return {"data": data}


async def document_semantic_tokens_full_delta(
    _ls: LspServer, params: dict | None
) -> dict | None:
    if params is None:
        return None
    uri = params["textDocument"]["uri"]
    previous_result_id = params.get("previousResultId", "")

    if global_state.wm_client is None:
        return None

    file_path = pygls_types_utils.uri_str_to_path(uri)
    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        return None

    try:
        response = await global_state.wm_client.run_action(
            action_source="fine_semantic_tokens.TextDocumentSemanticTokensDeltaAction",
            project=project_dir,
            params={
                "uri": uri,
                "previous_result_id": previous_result_id,
            },
            options={"trigger": "system", "devEnv": "ide"},
        )
    except Exception as error:
        logger.error(f"Error getting semantic token delta for {uri}: {error}")
        return None

    if response is None:
        return None

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None

    result_id = json_result.get("result_id")
    edits = json_result.get("edits") or []

    # If result_id is None, no handler could compute a delta — signal the client
    # to issue a full re-request by returning a delta with no resultId.
    response_dict: dict[str, Any] = {"edits": edits}
    if result_id is not None:
        response_dict["resultId"] = result_id
    return response_dict
