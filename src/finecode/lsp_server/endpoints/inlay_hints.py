from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode.lsp_server import global_state, pygls_types_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


def inlay_hint_params_to_dict(params: types.InlayHintParams) -> dict[str, Any]:
    return {
        "text_document": {
            "uri": params.text_document.uri,
        },
        "range": {
            "start": {
                "line": params.range.start.line + 1,
                "character": params.range.start.character,
            },
            "end": {
                "line": params.range.end.line + 1,
                "character": params.range.end.character,
            },
        },
    }


def dict_to_inlay_hint(raw: dict[str, Any]) -> types.InlayHint:
    return types.InlayHint(
        position=types.Position(
            line=raw["position"]["line"] - 1, character=raw["position"]["character"]
        ),
        label=raw["label"],
        kind=types.InlayHintKind(raw["kind"]),
        padding_left=raw.get("padding_left", False),
        padding_right=raw.get("padding_right", False),
    )


async def document_inlay_hint(
    ls: LanguageServer, params: types.InlayHintParams
) -> types.InlayHintResult:
    logger.trace(f"Document inlay hints requested: {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    if global_state.api_client is None:
        logger.error("Inlay hints requested but API client not connected")
        return None

    project_name = await global_state.api_client.find_project_for_file(str(file_path))
    if project_name is None:
        # Not all files belong to a project with this action — not an error.
        return []

    try:
        response = await global_state.api_client.run_action(
            action="text_document_inlay_hint",
            project=project_name,
            params=inlay_hint_params_to_dict(params),
            options={"trigger": "system", "dev_env": "ide"},
        )
    except Exception as error:
        logger.error(f"Error getting document inlay hints {file_path}: {error}")
        return None

    if response is None:
        return []

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return []

    hints = json_result.get("hints")
    return [dict_to_inlay_hint(hint) for hint in hints] if hints is not None else []


async def inlay_hint_resolve(
    ls: LanguageServer, params: types.InlayHint
) -> types.InlayHint | None:
    # TODO
    ...
