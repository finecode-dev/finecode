# TODO: handle all validation errors
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types
from pydantic.dataclasses import dataclass as pydantic_dataclass

from finecode.lsp_server import global_state, pygls_types_utils
from finecode_extension_api.actions import lint as lint_action


async def _find_project_dir_for_file(file_path: Path) -> str | None:
    """Return the absolute directory path of the project containing *file_path*.

    This helper delegates the lookup to the WM server via
    ``workspace/findProjectForFile``; the server applies the same logic that
    would otherwise live locally.  ``None`` is returned if the file does not
    belong to any known project.
    """
    # delegate the resolution to the WM server
    assert global_state.wm_client is not None, "WM client required for project lookup"
    project = await global_state.wm_client.find_project_for_file(str(file_path))
    return project


if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


def map_lint_message_to_diagnostic(
    lint_message: lint_action.LintMessage,
) -> types.Diagnostic:
    code_description_url = lint_message.code_description
    return types.Diagnostic(
        range=types.Range(
            types.Position(
                lint_message.range.start.line,
                lint_message.range.start.character,
            ),
            types.Position(
                lint_message.range.end.line,
                lint_message.range.end.character,
            ),
        ),
        message=lint_message.message,
        code=lint_message.code,
        code_description=(
            types.CodeDescription(href=code_description_url)
            if code_description_url is not None
            else None
        ),
        source=lint_message.source,
        severity=(
            types.DiagnosticSeverity(lint_message.severity)
            if lint_message.severity is not None
            else None
        ),
    )


async def document_diagnostic_with_full_result(
    file_path: Path,
) -> types.DocumentDiagnosticReport | None:
    logger.trace(f"Document diagnostic with full result: {file_path}")

    if global_state.wm_client is None:
        logger.error("Diagnostics requested but WM client not connected")
        return None

    project_dir = await _find_project_dir_for_file(file_path)
    if project_dir is None:
        logger.error(f"Cannot determine project for diagnostics: {file_path}")
        return None

    try:
        response = await global_state.wm_client.run_action(
            action="lint",
            project=project_dir,
            params={
                "target": "files",
                "file_paths": [str(file_path)],
            },
            options={"trigger": "system", "devEnv": "ide"},
        )
    except Exception as error:  # catching any runtime error from client
        # don't throw error because vscode after a few sequential errors will stop
        # requesting diagnostics until restart. Show user message instead
        logger.error(f"Diagnostics API request failed: {error}")
        return None

    if response is None:
        return None

    # use pydantic dataclass to convert dict to dataclass instance recursively
    # (default dataclass constructor doesn't handle nested items, it stores them just
    # as dict)
    json_result = (response.get("resultByFormat") or {}).get("json")
    if json_result is None:
        return None
    result_type = pydantic_dataclass(lint_action.LintRunResult)
    lint_result: lint_action.LintRunResult = result_type(**json_result)

    try:
        requested_file_messages = lint_result.messages.pop(str(file_path))
    except KeyError:
        requested_file_messages = []
    requested_files_diagnostic_items = [
        map_lint_message_to_diagnostic(lint_message)
        for lint_message in requested_file_messages
    ]
    response = types.RelatedFullDocumentDiagnosticReport(
        items=requested_files_diagnostic_items
    )

    related_files_diagnostics: dict[str, types.FullDocumentDiagnosticReport] = {}
    for file_path_str, file_lint_messages in lint_result.messages.items():
        file_report = types.FullDocumentDiagnosticReport(
            items=[
                map_lint_message_to_diagnostic(lint_message)
                for lint_message in file_lint_messages
            ]
        )
        file_path = Path(file_path_str)
        related_files_diagnostics[pygls_types_utils.path_to_uri_str(file_path)] = (
            file_report
        )
    response.related_documents = related_files_diagnostics

    logger.trace(f"Document diagnostic with full result for {file_path} finished")
    return response


async def document_diagnostic_with_partial_results(
    file_path: Path, partial_result_token: int | str
) -> None:
    logger.trace(f"Document diagnostic with partial results: {file_path}")

    if global_state.wm_client is None:
        logger.error("Diagnostics requested but WM client not connected")
        return None

    project_dir = await _find_project_dir_for_file(file_path)
    if project_dir is None:
        logger.error(f"Cannot determine project for diagnostics: {file_path}")
        return None

    # Store the expected response type for this token
    global_state.partial_result_tokens[partial_result_token] = ("lint", "document_diagnostic")

    try:
        await global_state.wm_client.request(
            "actions/runWithPartialResults",
            {
                "action": "lint",
                "project": project_dir,
                "params": {"file_paths": [str(file_path)]},
                "partialResultToken": partial_result_token,
                "options": {"resultFormats": ["json"], "trigger": "system", "devEnv": "ide"},
            },
        )
    except Exception as error:
        logger.error(f"Diagnostics API request failed: {error}")
    return None


async def document_diagnostic(
    ls: LanguageServer, params: types.DocumentDiagnosticParams
) -> types.DocumentDiagnosticReport | None:
    """
    LSP defines support of partial results in this endpoint, but testing of
    VSCode 1.99.3 showed that it never sends partial result token here.
    """
    logger.trace(f"Document diagnostic requested: {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    run_with_partial_results: bool = params.partial_result_token is not None
    try:
        if run_with_partial_results:
            assert params.partial_result_token is not None

            await document_diagnostic_with_partial_results(
                file_path=file_path, partial_result_token=params.partial_result_token
            )
            return None
        else:
            return await document_diagnostic_with_full_result(file_path=file_path)
    except Exception as e:
        logger.exception(e)

        # we ignore exceptions on diagnostics, because some IDEs will stop
        # calling diagnostics after certain number of failures(5 in case of VSCode).
        # This is not relevant for FineCode, because it can be a problem in action
        # handler, which can be disabled or reloaded without restarting the whole LSP
        # server(IDE requires restart of LSP to start calling diagnostics again).
        return None


async def run_workspace_diagnostic_with_partial_results(
    partial_result_token: str | int
):
    """Run lint with partial results on all projects.

    The WM server automatically runs the action in all relevant projects when
    the 'project' field is empty.
    """
    assert global_state.wm_client is not None, "WM client must be connected"

    # Store the expected response type for this token
    global_state.partial_result_tokens[partial_result_token] = ("lint", "workspace_diagnostic")

    try:
        # send request to WM server; notifications will trigger progress reporter
        await global_state.wm_client.request(
            "actions/runWithPartialResults",
            {
                "action": "lint",
                "project": "",  # empty project = all relevant projects
                "params": {"target": "project"},
                "partialResultToken": partial_result_token,
                "options": {"resultFormats": ["json"], "trigger": "system", "devEnv": "ide"},
            },
        )
    except Exception as error:
        logger.error(f"Workspace diagnostics API request failed: {error}")


async def workspace_diagnostic_with_partial_results(
    partial_result_token: str | int
) -> types.WorkspaceDiagnosticReport:
    """Request workspace diagnostics with partial results.

    Returns an empty report; the actual results arrive via notifications.
    """
    await run_workspace_diagnostic_with_partial_results(
        partial_result_token=partial_result_token
    )
    # lsprotocol allows None as return value, but then vscode throws error
    # 'cannot read items of null'. keep empty report instead
    return types.WorkspaceDiagnosticReport(items=[])


async def workspace_diagnostic_with_full_result() -> types.WorkspaceDiagnosticReport:
    """Run lint action on all projects via API and aggregate results.

    The WM server automatically runs in all relevant projects when 'project'
    field is empty.
    """
    assert global_state.wm_client is not None, "WM client must be connected"

    try:
        response = await global_state.wm_client.run_action(
            action="lint",
            project="",  # empty project = all relevant projects
            params={"target": "project"},
            options={"trigger": "system", "devEnv": "ide"},
        )
    except Exception as error:
        logger.error(f"Error in workspace diagnostic: {error}")
        return types.WorkspaceDiagnosticReport(items=[])

    if not response:
        return types.WorkspaceDiagnosticReport(items=[])

    # use pydantic dataclass to convert dict to dataclass instance recursively
    # (default dataclass constructor doesn't handle nested items, it stores them just
    # as dict)
    json_result = (response.get("resultByFormat") or {}).get("json")
    if not json_result:
        return types.WorkspaceDiagnosticReport(items=[])
    result_type = pydantic_dataclass(lint_action.LintRunResult)
    lint_result: lint_action.LintRunResult = result_type(**json_result)

    items: list[types.WorkspaceDocumentDiagnosticReport] = []
    for file_path_str, lint_messages in lint_result.messages.items():
        new_report = types.WorkspaceFullDocumentDiagnosticReport(
            uri=pygls_types_utils.path_to_uri_str(Path(file_path_str)),
            items=[
                map_lint_message_to_diagnostic(lint_message)
                for lint_message in lint_messages
            ],
        )
        items.append(new_report)

    # lsprotocol allows None as return value, but then vscode throws error
    # 'cannot read items of null'. keep empty report instead
    return types.WorkspaceDiagnosticReport(items=items)


async def _workspace_diagnostic(
    params: types.WorkspaceDiagnosticParams,
) -> types.WorkspaceDiagnosticReport | None:
    """Run workspace diagnostics for all projects via the WM server.

    The WM server automatically selects relevant projects when the 'project'
    field is empty.
    """
    assert global_state.wm_client is not None, "WM client must be connected"

    if params.partial_result_token is not None:
        # fire off partial‑result request and return an empty placeholder; the
        # progress reporter will handle streaming through notifications.
        await workspace_diagnostic_with_partial_results(
            partial_result_token=params.partial_result_token,
        )
        return types.WorkspaceDiagnosticReport(items=[])

    return await workspace_diagnostic_with_full_result()


async def workspace_diagnostic(
    ls: LanguageServer, params: types.WorkspaceDiagnosticParams
) -> types.WorkspaceDiagnosticReport | None:
    logger.trace(f"Workspace diagnostic requested: {params}")
    await global_state.server_initialized.wait()

    # catch all exceptions for 2 reasons:
    # - after a few sequential errors vscode will stop requesting diagnostics until
    # restart. Show user message instead
    # - pygls will cut information about exception in logs and it will be hard to
    #   understand it
    try:
        result = await _workspace_diagnostic(params)
    except Exception as exception:
        # TODO: user message
        logger.exception(exception)
        # lsprotocol allows None as return value, but then vscode throws error
        # 'cannot read items of null'. keep empty report instead
        return types.WorkspaceDiagnosticReport(items=[])

    logger.trace(f"Workspace diagnostic ended: {params}")
    return result
