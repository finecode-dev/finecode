from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager.server import global_state, proxy_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    logger.info(f"format document {params}")
    await global_state.server_initialized.wait()

    doc = ls.workspace.get_text_document(params.text_document.uri)
    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    # first check 'format' action, because it always replaces the whole content, then
    # TEXT_DOCUMENT_FORMATTING feature, it can replace also parts of document
    try:
        response = await proxy_utils.find_action_project_and_run_in_runner(
            file_path=file_path,
            action_name="format",
            params=[{"apply_on": file_path, "apply_on_text": doc.source}],
            ws_context=global_state.ws_context,
        )
    except Exception as error:  # TODO
        logger.error(f"Error document formatting {file_path}: {error}")
        return None

    if response.get("changed", True) is True:
        return [
            types.TextEdit(
                range=types.Range(
                    start=types.Position(0, 0),
                    end=types.Position(len(doc.lines), len(doc.lines[-1])),
                ),
                new_text=response["code"],
            )
        ]

    # TODO: restore
    # try:
    #     response = await proxy_utils.find_project_and_run_in_runner(
    #         file_path=file_path,
    #         method=types.TEXT_DOCUMENT_FORMATTING,
    #         params=params,
    #         response_type=list,  # TODO?
    #         ws_context=global_state.ws_context,
    #     )
    # except Exception as error: # TODO
    #     logger.error(f"Error document formatting {file_path}: {error}")
    #     return None

    # if response is not None and len(response) > 0:
    #     text_edit = response[0]
    #     assert isinstance(text_edit, types.TextEdit)
    #     if text_edit.range.end.character == -1 and text_edit.range.end.line == -1:
    #         text_edit.range.end = types.Position(
    #             line=len(doc.lines),
    #             character=len(doc.lines[-1])
    #         )

    return response


async def format_range(ls: LanguageServer, params: types.DocumentRangeFormattingParams):
    logger.info(f"format range {params}")
    await global_state.server_initialized.wait()

    return []


async def format_ranges(ls: LanguageServer, params: types.DocumentRangesFormattingParams):
    logger.info(f"format ranges {params}")
    await global_state.server_initialized.wait()

    return []
