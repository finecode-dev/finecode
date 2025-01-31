from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager.server import global_state, proxy_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer


def map_lint_message_dict_to_diagnostic(lint_message: dict[str, Any]) -> types.Diagnostic:
    return types.Diagnostic(
        range=types.Range(
            types.Position(
                lint_message["range"]["start"]["line"],
                lint_message["range"]["start"]["character"],
            ),
            types.Position(
                lint_message["range"]["end"]["line"],
                lint_message["range"]["end"]["character"],
            ),
        ),
        message=lint_message["message"],
        code=lint_message.get("code", None),
        code_description=lint_message.get("code_description", None),
        source=lint_message.get("source", None),
        severity=(
            types.DiagnosticSeverity(lint_message.get("severity", None))
            if lint_message.get("severity", None) is not None
            else None
        ),
    )


async def document_diagnostic(
    ls: LanguageServer, params: types.DocumentDiagnosticParams
) -> types.DocumentDiagnosticReport | None:
    logger.trace(f"Document diagnostic requested: {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    # TODO: check whether 'lint' action is available and enabled
    # TODO: file is read from file system. If it was changed and not saved in IDE, changes are ignored.
    #       read file using LSP API
    response = await proxy_utils.find_action_project_and_run_in_runner(
        file_path=file_path,
        action_name="lint",
        params=[{"apply_on": file_path, "apply_on_text": ""}],
        ws_context=global_state.ws_context,
    )

    if response is None:
        return None

    lint_messages: dict[str, Any] = response.get("messages", {})

    try:
        requested_file_messages = lint_messages.pop(str(file_path))
    except KeyError:
        requested_file_messages = []
    requested_files_diagnostic_items = [
        map_lint_message_dict_to_diagnostic(lint_message)
        for lint_message in requested_file_messages
    ]
    response = types.RelatedFullDocumentDiagnosticReport(items=requested_files_diagnostic_items)

    related_files_diagnostics: dict[str, types.FullDocumentDiagnosticReport] = {}
    for file_path_str, file_lint_messages in lint_messages.items():
        file_report = types.FullDocumentDiagnosticReport(
            items=[
                map_lint_message_dict_to_diagnostic(lint_message)
                for lint_message in file_lint_messages
            ]
        )
        related_files_diagnostics[pygls_types_utils.path_to_uri_str(file_path_str)] = file_report
    response.related_documents = related_files_diagnostics

    # try:
    #     response = await proxy_utils.find_project_and_run_in_runner(
    #         file_path=file_path,
    #         method=types.TEXT_DOCUMENT_DIAGNOSTIC,
    #         params=params,
    #         response_type=types.DocumentDiagnosticReport,
    #         ws_context=global_state.ws_context,
    #     )
    # except Exception as error: # TODO
    #     logger.error(f"Error document diagnostic {file_path}: {error}")
    #     return None

    return response


async def workspace_diagnostic(
    ls: LanguageServer, params: types.WorkspaceDiagnosticParams
) -> types.WorkspaceDiagnosticReport | None:
    # TODO: partial responses
    logger.trace(f"Workspace diagnostic requested: {params}")

    # find which runner is responsible for which files
    # currently FineCode supports only raw python files, find them in each ws project
    # TODO: build tree of projects
    #       if both parent and child projects have lint action, exclude files of chid from parent
    # check which runners are active and run in them

    responses = await proxy_utils.run_action_in_all_runners(
        action_name="lint",
        params=params,
        ws_context=global_state.ws_context,
    )

    merged_response: types.WorkspaceDiagnosticReport | None = None
    for response in responses:
        if response is None:
            continue
        else:
            ...  # TODO

    # responses = await proxy_utils.run_in_all_runners(
    #     method=types.WORKSPACE_DIAGNOSTIC,
    #     params=params,
    #     response_type=types.WorkspaceDiagnosticReport,
    #     ws_context=global_state.ws_context,
    # )

    # merged_response: types.WorkspaceDiagnosticReport | None = None
    # for response in responses:
    #     if response is None:
    #         continue

    #     if merged_response is None:
    #         merged_response = types.WorkspaceDiagnosticReport(items=response.items)
    #     else:
    #         merged_response.items.extend(response.items)

    return merged_response
