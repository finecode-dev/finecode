from __future__ import annotations

from typing import TYPE_CHECKING

from finecode.lsp_server import global_state, pygls_types_utils
from finecode_extension_api.actions import format_files as format_files_action
from loguru import logger
from lsprotocol import types
from pydantic.dataclasses import dataclass as pydantic_dataclass

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


async def format_document(ls: LanguageServer, params: types.DocumentFormattingParams):
    logger.info(f"format document {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    if global_state.api_client is None:
        logger.error("Formatting requested but API client not connected")
        return None

    project_name = await global_state.api_client.find_project_for_file(str(file_path))
    if project_name is None:
        logger.error(f"Cannot determine project for formatting: {file_path}")
        return []

    try:
        response = await global_state.api_client.run_action(
            action="format",
            project=project_name,
            params={"file_paths": [str(file_path)], "save": False, "target": "files"},
            options={"trigger": "user", "dev_env": "ide"},
        )
    except Exception as error:
        logger.error(f"Error document formatting {file_path}: {error}")
        return None

    if response is None:
        return []

    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return []

    result_type = pydantic_dataclass(format_files_action.FormatFilesRunResult)
    format_result: format_files_action.FormatFilesRunResult = result_type(**json_result)

    response_for_file = format_result.result_by_file_path.get(file_path)
    if response_for_file is None:
        return []

    if response_for_file.changed is True:
        doc = ls.workspace.get_text_document(params.text_document.uri)
        return [
            types.TextEdit(
                range=types.Range(
                    start=types.Position(0, 0),
                    end=types.Position(len(doc.lines), len(doc.lines[-1])),
                ),
                new_text=response_for_file.code,
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
