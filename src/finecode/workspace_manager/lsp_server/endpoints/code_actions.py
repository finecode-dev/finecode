from __future__ import annotations

from typing import TYPE_CHECKING

# from loguru import logger
from loguru import logger
from lsprotocol import types

# from finecode import pygls_types_utils, lsp_types
# from finecode.workspace_manager.server import global_state, proxy_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def document_code_action(
    ls: LanguageServer, params: types.CodeActionParams
) -> types.CodeActionResult:
    logger.debug(f"{params}")
    return [
        types.CodeAction(
            title="Make Private", kind=types.CodeActionKind.RefactorRewrite
        )
    ]
    # file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)
    # payload = lsp_types.CodeActionPayload(
    #     text_document=lsp_types.TextDocumentIdentifier(uri=params.text_document.uri),
    #     range=lsp_types.Range(
    #         start=lsp_types.Position(params.range.start.line,
    # params.range.start.character),
    #         end=lsp_types.Position(params.range.end.line, params.range.end.character)
    #     )
    # )
    # try:
    #     response = await proxy_utils.find_action_project_and_run_in_runner(
    #         file_path=file_path,
    #         action_name="text_document_code_action",
    #         params=[payload.model_dump()],
    #         ws_context=global_state.ws_context,
    #     )
    # except Exception as error:  # TODO
    #     logger.error(f"Error getting document inlay hints {file_path}: {error}")
    #     return None

    # hints = response.get('hints', None)
    # return [dict_to_inlay_hint(hint) for hint in hints] if hints is not None else None
    # return [
    #     types.CodeAction(
    #         title="Make private",
    #         diagnostics=[
    #             types.Diagnostic(
    #                 range=types.Range(
    #                     start=types.Position(1, 1), end=types.Position(1, 5)
    #                 ),
    #                 message="public",
    #                 severity=types.DiagnosticSeverity.Information,
    #             )
    #         ],
    #     )
    # ]


async def code_action_resolve(
    ls: LanguageServer, params: types.CodeAction
) -> types.CodeAction: ...
