"""Document synchronization handlers for the API server.

Handles document lifecycle notifications (opened, closed, changed) and forwards
them to affected extension runners.
"""

from __future__ import annotations

import asyncio
import pathlib
from loguru import logger

from finecode.api_server import context, domain


async def handle_documents_opened(
    params: dict | None, ws_context: context.WorkspaceContext
) -> None:
    """Handle document opened notification. Forward to affected runners."""
    if params is None:
        return

    from finecode.api_server.runner import runner_client

    uri = params.get("uri")
    version = params.get("version")
    if not uri:
        return

    file_path = pathlib.Path(uri.replace("file://", ""))
    projects_paths = [
        project_path
        for project_path, project in ws_context.ws_projects.items()
        if project.status == domain.ProjectStatus.CONFIG_VALID
        and file_path.is_relative_to(project_path)
    ]

    document_info = domain.TextDocumentInfo(uri=uri, version=str(version or ""))
    try:
        async with asyncio.TaskGroup() as tg:
            for project_path in projects_paths:
                runners_by_env = ws_context.ws_projects_extension_runners.get(
                    project_path, {}
                )
                for runner in runners_by_env.values():
                    if runner.status == runner_client.RunnerStatus.RUNNING:
                        tg.create_task(
                            runner_client.notify_document_did_open(
                                runner=runner, document_info=document_info
                            )
                        )
    except ExceptionGroup as eg:
        for exception in eg.exceptions:
            logger.exception(exception)
        logger.error(f"Error while sending opened document: {eg}")


async def handle_documents_closed(
    params: dict | None, ws_context: context.WorkspaceContext
) -> None:
    """Handle document closed notification. Forward to affected runners."""
    if params is None:
        return

    from finecode.api_server.runner import runner_client

    uri = params.get("uri")
    if not uri:
        return

    file_path = pathlib.Path(uri.replace("file://", ""))
    projects_paths = [
        project_path
        for project_path, project in ws_context.ws_projects.items()
        if project.status == domain.ProjectStatus.CONFIG_VALID
        and file_path.is_relative_to(project_path)
    ]

    try:
        async with asyncio.TaskGroup() as tg:
            for project_path in projects_paths:
                runners_by_env = ws_context.ws_projects_extension_runners.get(
                    project_path, {}
                )
                for runner in runners_by_env.values():
                    if runner.status != runner_client.RunnerStatus.RUNNING:
                        logger.trace(
                            f"Runner {runner.readable_id} is not running, skip it"
                        )
                        continue

                    tg.create_task(
                        runner_client.notify_document_did_close(
                            runner=runner, document_uri=uri
                        )
                    )
    except ExceptionGroup as e:
        logger.error(f"Error while sending closed document: {e}")


async def handle_documents_changed(
    params: dict | None, ws_context: context.WorkspaceContext
) -> None:
    """Handle document changed notification. Forward to affected runners."""
    if params is None:
        return

    from finecode.api_server.runner import runner_client

    uri = params.get("uri")
    version = params.get("version")
    content_changes = params.get("contentChanges", [])
    if not uri:
        return

    file_path = pathlib.Path(uri.replace("file://", ""))
    projects_paths = [
        project_path
        for project_path, project in ws_context.ws_projects.items()
        if project.status == domain.ProjectStatus.CONFIG_VALID
        and file_path.is_relative_to(project_path)
    ]

    # Convert camelCase content changes back to snake_case for runner_client
    mapped_changes = []
    for change in content_changes:
        if "range" in change:
            # TextDocumentContentChangePartial
            mapped_change = runner_client.TextDocumentContentChangePartial(
                range=runner_client.Range(
                    start=runner_client.Position(
                        line=change["range"]["start"]["line"],
                        character=change["range"]["start"]["character"],
                    ),
                    end=runner_client.Position(
                        line=change["range"]["end"]["line"],
                        character=change["range"]["end"]["character"],
                    ),
                ),
                text=change.get("text", ""),
                range_length=change.get("rangeLength"),
            )
            mapped_changes.append(mapped_change)
        else:
            # TextDocumentContentChangeWholeDocument
            mapped_change = runner_client.TextDocumentContentChangeWholeDocument(
                text=change.get("text", "")
            )
            mapped_changes.append(mapped_change)

    change_params = runner_client.DidChangeTextDocumentParams(
        text_document=runner_client.VersionedTextDocumentIdentifier(
            version=version, uri=uri
        ),
        content_changes=mapped_changes,
    )

    try:
        async with asyncio.TaskGroup() as tg:
            for project_path in projects_paths:
                runners_by_env = ws_context.ws_projects_extension_runners.get(
                    project_path, {}
                )
                for runner in runners_by_env.values():
                    if runner.status != runner_client.RunnerStatus.RUNNING:
                        logger.trace(
                            f"Runner {runner.readable_id} is not running, skip it"
                        )
                        continue

                    tg.create_task(
                        runner_client.notify_document_did_change(
                            runner=runner, change_params=change_params
                        )
                    )
    except ExceptionGroup as e:
        logger.error(f"Error while sending changed document: {e}")
