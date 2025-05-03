import asyncio
from pathlib import Path

from loguru import logger
from lsprotocol import types
from pygls.lsp.server import LanguageServer

from finecode.workspace_manager import domain
from finecode.workspace_manager.lsp_server import global_state
from finecode.workspace_manager.runner import runner_client


async def document_did_open(
    ls: LanguageServer, params: types.DidOpenTextDocumentParams
):
    logger.trace(f"Document did open: {params.text_document.uri}")
    global_state.ws_context.opened_documents[params.text_document.uri] = (
        domain.TextDocumentInfo(
            uri=params.text_document.uri, version=str(params.text_document.version)
        )
    )

    file_path = Path(params.text_document.uri.replace("file://", ""))
    projects_paths = [
        project_path
        for project_path, project in global_state.ws_context.ws_projects.items()
        if project.status == domain.ProjectStatus.RUNNING
        and file_path.is_relative_to(project_path)
    ]

    document_info = domain.TextDocumentInfo(
        uri=params.text_document.uri, version=str(params.text_document.version)
    )
    try:
        async with asyncio.TaskGroup() as tg:
            for project_path in projects_paths:
                runner = global_state.ws_context.ws_projects_extension_runners[
                    project_path
                ]
                tg.create_task(
                    runner_client.notify_document_did_open(
                        runner=runner, document_info=document_info
                    )
                )
    except ExceptionGroup as e:
        logger.error(f"Error while sending opened document: {e}")


async def document_did_close(
    ls: LanguageServer, params: types.DidCloseTextDocumentParams
):
    logger.trace(f"Document did close: {params.text_document.uri}")
    try:
        del global_state.ws_context.opened_documents[params.text_document.uri]
    except KeyError:
        logger.error(
            f"Document not found in opened documents: {params.text_document.uri}"
        )
        return

    file_path = Path(params.text_document.uri.replace("file://", ""))
    projects_paths = [
        project_path
        for project_path, project in global_state.ws_context.ws_projects.items()
        if project.status == domain.ProjectStatus.RUNNING
        and file_path.is_relative_to(project_path)
    ]

    try:
        async with asyncio.TaskGroup() as tg:
            for project_path in projects_paths:
                runner = global_state.ws_context.ws_projects_extension_runners[
                    project_path
                ]
                tg.create_task(
                    runner_client.notify_document_did_close(
                        runner=runner, document_uri=params.text_document.uri
                    )
                )
    except ExceptionGroup as e:
        logger.error(f"Error while sending closed document: {e}")


async def document_did_save(
    ls: LanguageServer, params: types.DidSaveTextDocumentParams
):
    logger.trace(f"Document did save: {params}")


async def document_did_change(
    ls: LanguageServer, params: types.DidChangeTextDocumentParams
):
    global_state.ws_context.opened_documents[params.text_document.uri].version = (
        params.text_document.version
    )
