from loguru import logger
from lsprotocol import types
from pygls.lsp.server import LanguageServer

from finecode.lsp_server import global_state


async def document_did_open(
    ls: LanguageServer, params: types.DidOpenTextDocumentParams
):
    logger.trace(f"Document did open: {params.text_document.uri}")
    await global_state.server_initialized.wait()

    if global_state.api_client is None:
        raise Exception("API server not connected")

    await global_state.api_client.notify_document_opened(
        uri=params.text_document.uri, version=params.text_document.version
    )


async def document_did_close(
    ls: LanguageServer, params: types.DidCloseTextDocumentParams
):
    logger.trace(f"Document did close: {params.text_document.uri}")
    await global_state.server_initialized.wait()

    if global_state.api_client is None:
        raise Exception("API server not connected")

    await global_state.api_client.notify_document_closed(
        uri=params.text_document.uri
    )


async def document_did_save(
    ls: LanguageServer, params: types.DidSaveTextDocumentParams
):
    logger.trace(f"Document did save: {params}")
    await global_state.server_initialized.wait()


async def document_did_change(
    ls: LanguageServer, params: types.DidChangeTextDocumentParams
):
    logger.trace(f"Document did change: {params.text_document.uri}")
    await global_state.server_initialized.wait()

    if global_state.api_client is None:
        raise Exception("API server not connected")

    # Convert content changes to API format (camelCase)
    content_changes = []
    for change in params.content_changes:
        if isinstance(change, types.TextDocumentContentChangePartial):
            content_changes.append(
                {
                    "range": {
                        "start": {
                            "line": change.range.start.line,
                            "character": change.range.start.character,
                        },
                        "end": {
                            "line": change.range.end.line,
                            "character": change.range.end.character,
                        },
                    },
                    "text": change.text,
                    "rangeLength": change.range_length,
                }
            )
        elif isinstance(change, types.TextDocumentContentChangeWholeDocument):
            content_changes.append({"text": change.text})
        else:
            logger.error(
                f"Got unsupported content change from LSP client: {type(change)}, skip it"
            )
            continue

    await global_state.api_client.notify_document_changed(
        uri=params.text_document.uri,
        version=params.text_document.version,
        content_changes=content_changes,
    )
