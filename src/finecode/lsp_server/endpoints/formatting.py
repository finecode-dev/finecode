from __future__ import annotations

from typing import TYPE_CHECKING

from finecode.lsp_server import global_state, pygls_types_utils
from finecode_extension_api.actions.code_quality import format_files_action
from loguru import logger
from lsprotocol import types
from pydantic.dataclasses import dataclass as pydantic_dataclass

if TYPE_CHECKING:
    from finecode.lsp_server.lsp_server import LspServer


async def format_document(_ls: LspServer, params: types.DocumentFormattingParams):
    logger.info(f"format document {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    if global_state.wm_client is None:
        logger.error("Formatting requested but WM client not connected")
        return None

    project_dir = await global_state.wm_client.find_project_for_file(str(file_path))
    if project_dir is None:
        logger.error(f"Cannot determine project for formatting: {file_path}")
        return []

    file_uri = file_path.as_uri()

    try:
        response = await global_state.wm_client.run_action(
            action="format",
            project=project_dir,
            params={"file_paths": [file_uri], "save": False, "target": "files"},
            options={"trigger": "user", "devEnv": "ide"},
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

    response_for_file = format_result.result_by_file_path.get(file_uri)
    if response_for_file is None:
        return []

    if response_for_file.changed is True:
        return [
            types.TextEdit(
                range=types.Range(
                    start=types.Position(0, 0),
                    end=types.Position(999999, 0),
                ),
                new_text=response_for_file.code,
            )
        ]

    return []


async def format_range(_ls: LspServer, params: types.DocumentRangeFormattingParams):
    logger.info(f"format range {params}")
    await global_state.server_initialized.wait()
    # TODO
    return []


async def format_ranges(
    _ls: LspServer, params: types.DocumentRangesFormattingParams
):
    logger.info(f"format ranges {params}")
    await global_state.server_initialized.wait()
    # TODO
    return []
