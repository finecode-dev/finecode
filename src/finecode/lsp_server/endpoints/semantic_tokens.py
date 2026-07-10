from __future__ import annotations

import difflib
import uuid
from typing import TYPE_CHECKING, Any

from loguru import logger

from finecode.lsp_server import global_state, pygls_types_utils
from finecode.lsp_server.endpoints import _cancellation
from fine_semantic_tokens.text_document_semantic_tokens_action import (
    SEMANTIC_TOKEN_TYPES,
    SEMANTIC_TOKEN_MODIFIERS,
    SemanticToken,
)

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


# uri -> (result_id, wire-format encoded token data) for the most recent full
# response. Only full requests read/write this cache; delta computation diffs
# against it. Populated by document_semantic_tokens_full, consulted and
# refreshed by document_semantic_tokens_full_delta, evicted by
# clear_cache_for_uri (called from document_did_close).
_full_tokens_cache: dict[str, tuple[str, list[int]]] = {}


def clear_cache_for_uri(uri: str) -> None:
    _full_tokens_cache.pop(uri, None)


def _diff_semantic_tokens_data(
    old_data: list[int], new_data: list[int]
) -> list[dict[str, Any]]:
    """Diff two wire-format semantic token arrays into LSP delta edits.

    Tokens are grouped into their natural 5-integer unit before diffing so
    that a single token move/change becomes one edit instead of five
    unrelated integer edits. SequenceMatcher.get_opcodes() yields
    non-overlapping, ascending ranges over the *old* sequence, which matches
    how LSP clients apply SemanticTokensEdit[]: each edit's start/deleteCount
    refers to the original array, not one progressively mutated by prior
    edits in the same response.
    """
    old_tokens = [tuple(old_data[i : i + 5]) for i in range(0, len(old_data), 5)]
    new_tokens = [tuple(new_data[i : i + 5]) for i in range(0, len(new_data), 5)]
    matcher = difflib.SequenceMatcher(a=old_tokens, b=new_tokens, autojunk=False)

    edits: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        edit: dict[str, Any] = {"start": i1 * 5, "deleteCount": (i2 - i1) * 5}
        if j2 > j1:
            data: list[int] = []
            for tok in new_tokens[j1:j2]:
                data.extend(tok)
            edit["data"] = data
        edits.append(edit)
    return edits


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
        _cancellation.reraise_if_cancelled(error, context=f"Error getting semantic tokens for {uri}")
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
    result_id = uuid.uuid4().hex
    _full_tokens_cache[uri] = (result_id, data)
    return {"resultId": result_id, "data": data}


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

    new_data = await _run_full_or_range(uri, range_dict=None)
    if new_data is None:
        return None

    new_result_id = uuid.uuid4().hex
    cached = _full_tokens_cache.get(uri)
    _full_tokens_cache[uri] = (new_result_id, new_data)

    if cached is None or cached[0] != previous_result_id:
        # Unknown/stale baseline (first request, server restart, or a racing
        # full request already replaced it) — fall back to a full response,
        # which is a valid SemanticTokensDelta | SemanticTokens result per spec.
        return {"resultId": new_result_id, "data": new_data}

    old_data = cached[1]
    edits = _diff_semantic_tokens_data(old_data, new_data)
    return {"resultId": new_result_id, "edits": edits}
