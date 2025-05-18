from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager import find_project, proxy_utils
from finecode.workspace_manager.lsp_server import global_state

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
    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)
    try:
        response = await proxy_utils.find_action_project_and_run(
            file_path=file_path,
            action_name="text_document_inlay_hint",
            params=inlay_hint_params_to_dict(params),
            ws_context=global_state.ws_context,
        )
    except find_project.FileHasNotActionException:
        # ignore this exception because client requests inlay hints for all workspace
        # files and not neccessary all projects in ws have this action. So this is not
        # an real error.
        return []
    except Exception as error:  # TODO
        logger.error(f"Error getting document inlay hints {file_path}: {error}")
        return None

    if response is None:
        return []

    hints = response.result.get("hints", None)
    return [dict_to_inlay_hint(hint) for hint in hints] if hints is not None else None


async def inlay_hint_resolve(
    ls: LanguageServer, params: types.InlayHint
) -> types.InlayHint | None: ...
