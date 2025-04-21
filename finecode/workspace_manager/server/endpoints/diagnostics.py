# TODO: handle all validation errors
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager import domain, project_analyzer, proxy_utils
from finecode.workspace_manager.runner import runner_client
from finecode.workspace_manager.server import global_state
from finecode_extension_api.actions import lint as lint_action

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer

    from finecode.workspace_manager.runner import runner_info


def map_lint_message_dict_to_diagnostic(
    lint_message: lint_action.LintMessage,
) -> types.Diagnostic:
    code_description_url = lint_message.code_description
    return types.Diagnostic(
        range=types.Range(
            types.Position(
                lint_message.range.start.line - 1,
                lint_message.range.start.character,
            ),
            types.Position(
                lint_message.range.end.line - 1,
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
    try:
        response = await proxy_utils.find_action_project_and_run(
            file_path=file_path,
            action_name="lint",
            # TODO: use payload class
            params={
                "file_paths": [file_path],
            },
            ws_context=global_state.ws_context,
        )
    except proxy_utils.ActionRunFailed as error:
        # don't throw error because vscode after a few sequential errors will stop
        # requesting diagnostics until restart. Show user message instead
        logger.error(str(error))  # TODO: user message
        return None

    if response is None:
        return None

    lint_result: lint_action.LintRunResult = lint_action.LintRunResult(**response)

    try:
        requested_file_messages = lint_result.messages.pop(str(file_path))
    except KeyError:
        requested_file_messages = []
    requested_files_diagnostic_items = [
        map_lint_message_dict_to_diagnostic(lint_message)
        for lint_message in requested_file_messages
    ]
    response = types.RelatedFullDocumentDiagnosticReport(
        items=requested_files_diagnostic_items
    )

    related_files_diagnostics: dict[str, types.FullDocumentDiagnosticReport] = {}
    for file_path_str, file_lint_messages in lint_result.messages.items():
        file_report = types.FullDocumentDiagnosticReport(
            items=[
                map_lint_message_dict_to_diagnostic(lint_message)
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
    assert global_state.progress_reporter is not None, (
        "LSP Server in Workspace Manager was incorrectly initialized:"
        " progress reporter not registered"
    )

    try:
        async with proxy_utils.find_action_project_and_run_with_partial_results(
            file_path=file_path,
            action_name="lint",
            # TODO: use payload class
            params={
                "file_paths": [file_path],
            },
            partial_result_token=partial_result_token,
            ws_context=global_state.ws_context,
        ) as response:
            # TODO: order of partial results is important in LSP? Order here?
            async for partial_response in response:
                # TODO: convert partial response to LSP type
                # global_state.progress_reporter(partial_result_token, partial_response)
                logger.debug(f"---> {partial_response}")
    except proxy_utils.ActionRunFailed as error:
        # don't throw error because vscode after a few sequential errors will stop
        # requesting diagnostics until restart. Show user message instead
        logger.error(str(error))  # TODO: user message

    return None


async def document_diagnostic(
    ls: LanguageServer, params: types.DocumentDiagnosticParams
) -> types.DocumentDiagnosticReport | None:
    logger.trace(f"Document diagnostic requested: {params}")
    await global_state.server_initialized.wait()

    file_path = pygls_types_utils.uri_str_to_path(params.text_document.uri)

    run_with_partial_results: bool = params.partial_result_token is not None
    try:
        if run_with_partial_results:
            return await document_diagnostic_with_partial_results(
                file_path=file_path, partial_result_token=params.partial_result_token
            )
        else:
            return await document_diagnostic_with_full_result(file_path=file_path)
    except Exception as e:
        logger.exception(e)


@dataclass
class LintActionExecInfo:
    runner: runner_info.ExtensionRunnerInfo
    action_name: str
    request_data: dict[str, str | list[str]] = field(default_factory=dict)


async def run_workspace_diagnostic_with_partial_results(
    exec_info: LintActionExecInfo, partial_result_token: str | int
):
    assert global_state.progress_reporter is not None

    try:
        async with proxy_utils.run_with_partial_results(
            action_name="lint",
            params=exec_info.request_data,
            partial_result_token=partial_result_token,
            runner=exec_info.runner,
        ) as response:
            # TODO: order of partial results is important in LSP? Order here?
            async for partial_response in response:
                lint_subresult = lint_action.LintRunResult(**partial_response)
                lsp_subresult = types.WorkspaceDiagnosticReportPartialResult(
                    items=[
                        types.WorkspaceFullDocumentDiagnosticReport(
                            uri=pygls_types_utils.path_to_uri_str(Path(file_path_str)),
                            items=[
                                map_lint_message_dict_to_diagnostic(lint_message)
                                for lint_message in lint_messages
                            ],
                        )
                        for file_path_str, lint_messages in lint_subresult.messages.items()
                    ]
                )
                global_state.progress_reporter(partial_result_token, lsp_subresult)
    except proxy_utils.ActionRunFailed as error:
        # don't throw error because vscode after a few sequential errors will stop
        # requesting diagnostics until restart. Show user message instead
        logger.error(str(error))  # TODO: user message


async def workspace_diagnostic_with_partial_results(
    exec_infos: list[LintActionExecInfo], partial_result_token: str | int
):
    try:
        async with asyncio.TaskGroup() as tg:
            for exec_info in exec_infos:
                tg.create_task(
                    run_workspace_diagnostic_with_partial_results(
                        exec_info=exec_info, partial_result_token=partial_result_token
                    )
                )
    except ExceptionGroup as eg:
        logger.error(f"Error in workspace diagnostic: {eg.exceptions}")

    # lsprotocol allows None as return value, but then vscode throws error
    # 'cannot read items of null'. keep empty report instead
    return types.WorkspaceDiagnosticReport(items=[])


async def workspace_diagnostic_with_full_result(exec_infos: list[LintActionExecInfo]):
    send_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for exec_info in exec_infos:
                task = tg.create_task(
                    runner_client.run_action(
                        runner=exec_info.runner,
                        action_name=exec_info.action_name,
                        params=exec_info.request_data,
                    )
                )
                send_tasks.append(task)
    except ExceptionGroup as eg:
        logger.error(f"Error in workspace diagnostic: {eg.exceptions}")

    responses = [task.result() for task in send_tasks]

    items: list[types.WorkspaceDocumentDiagnosticReport] = []
    for response in responses:
        if response is None:
            continue
        else:
            lint_result = lint_action.LintRunResult(**response)
            for file_path_str, lint_messages in lint_result.messages.items():
                new_report = types.WorkspaceFullDocumentDiagnosticReport(
                    uri=pygls_types_utils.path_to_uri_str(Path(file_path_str)),
                    items=[
                        map_lint_message_dict_to_diagnostic(lint_message)
                        for lint_message in lint_messages
                    ],
                )
                items.append(new_report)

    # lsprotocol allows None as return value, but then vscode throws error
    # 'cannot read items of null'. keep empty report instead
    return types.WorkspaceDiagnosticReport(items=items)


async def workspace_diagnostic(
    ls: LanguageServer, params: types.WorkspaceDiagnosticParams
) -> types.WorkspaceDiagnosticReport | None:
    logger.trace(f"Workspace diagnostic requested: {params}")
    await global_state.server_initialized.wait()

    # find which runner is responsible for which files
    # currently FineCode supports only raw python files, find them in each ws project
    # exclude projects without finecode
    # if both parent and child projects have lint action, exclude files of chid from
    # parent
    # check which runners are active and run in them

    projects = global_state.ws_context.ws_projects
    relevant_projects: dict[Path, domain.Project] = {
        path: project
        for path, project in projects.items()
        if project.status != domain.ProjectStatus.NO_FINECODE
    }
    exec_info_by_project_dir_path: dict[Path, LintActionExecInfo] = {}
    # exclude projects without lint action
    for project_dir_path, project_def in relevant_projects.copy().items():
        if project_def.status != domain.ProjectStatus.RUNNING:
            # projects that are not running, have no actions. Files of those projects
            # will be not processed because we don't know whether it has one of expected
            # actions
            continue

        actions_names: list[str] = [action.name for action in project_def.actions]
        if "lint" in actions_names:
            action_name = "lint"
        else:
            del relevant_projects[project_dir_path]
            continue

        runner = global_state.ws_context.ws_projects_extension_runners[project_dir_path]
        exec_info_by_project_dir_path[project_dir_path] = LintActionExecInfo(
            runner=runner, action_name=action_name
        )

    relevant_projects_paths: list[Path] = list(relevant_projects.keys())
    # assign files to projects
    files_by_projects: dict[Path, list[Path]] = project_analyzer.get_files_by_projects(
        projects_dirs_paths=relevant_projects_paths
    )

    for project_dir_path, files_for_runner in files_by_projects.items():
        project = global_state.ws_context.ws_projects[project_dir_path]
        if project.status != domain.ProjectStatus.RUNNING:
            logger.warning(
                f"Runner of project {project_dir_path} is not running,"
                " lint in it will not be executed"
            )
            continue

        exec_info = exec_info_by_project_dir_path[project_dir_path]
        if exec_info.action_name == "lint":
            # TODO: use payload class
            exec_info.request_data = {
                "file_paths": [file_path.as_posix() for file_path in files_for_runner],
            }

    exec_infos = list(exec_info_by_project_dir_path.values())
    run_with_partial_results: bool = params.partial_result_token is not None

    if run_with_partial_results:
        return await workspace_diagnostic_with_partial_results(
            exec_infos=exec_infos, partial_result_token=params.partial_result_token
        )
    else:
        return await workspace_diagnostic_with_full_result(exec_infos=exec_infos)
