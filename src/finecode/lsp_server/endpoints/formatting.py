from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.services import run_service
from finecode.lsp_server import global_state

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    logger.info(f"format document {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    try:
        response = await run_service.find_action_project_and_run(
            file_path=file_path,
            action_name="format",
            params={"file_paths": [file_path], "save": False},
            ws_context=global_state.ws_context,
        )
    except Exception as error:  # TODO
        logger.error(f"Error document formatting {file_path}: {error}")
        return None

    if response is None:
        return []

    response_for_file = response.result.get("result_by_file_path", {}).get(
        str(file_path), None
    )
    if response_for_file is None:
        return []

    if response_for_file.get("changed", True) is True:
        doc = ls.workspace.get_text_document(params.text_document.uri)
        return [
            types.TextEdit(
                range=types.Range(
                    start=types.Position(0, 0),
                    end=types.Position(len(doc.lines), len(doc.lines[-1])),
                ),
                new_text=response_for_file["code"],
            )
        ]

    return []


async def format_range(ls: LanguageServer, params: types.DocumentRangeFormattingParams):
    logger.info(f"format range {params}")
    await global_state.server_initialized.wait()
    # TODO
    return []


async def format_ranges(
    ls: LanguageServer, params: types.DocumentRangesFormattingParams
):
    logger.info(f"format ranges {params}")
    await global_state.server_initialized.wait()
    # TODO
    return []
