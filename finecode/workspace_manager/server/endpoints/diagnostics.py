from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager import domain, project_analyzer
from finecode.workspace_manager.runner import runner_client
from finecode.workspace_manager.server import global_state, proxy_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer

    from finecode.workspace_manager.runner import runner_info


def map_lint_message_dict_to_diagnostic(
    lint_message: dict[str, Any],
) -> types.Diagnostic:
    return types.Diagnostic(
        range=types.Range(
            types.Position(
                lint_message["range"]["start"]["line"] - 1,
                lint_message["range"]["start"]["character"],
            ),
            types.Position(
                lint_message["range"]["end"]["line"] - 1,
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

    response = await proxy_utils.find_action_project_and_run_in_runner(
        file_path=file_path,
        action_name="lint",
        params=[{"file_path": file_path}],
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
    response = types.RelatedFullDocumentDiagnosticReport(
        items=requested_files_diagnostic_items
    )

    related_files_diagnostics: dict[str, types.FullDocumentDiagnosticReport] = {}
    for file_path_str, file_lint_messages in lint_messages.items():
        file_report = types.FullDocumentDiagnosticReport(
            items=[
                map_lint_message_dict_to_diagnostic(lint_message)
                for lint_message in file_lint_messages
            ]
        )
        related_files_diagnostics[pygls_types_utils.path_to_uri_str(file_path_str)] = (
            file_report
        )
    response.related_documents = related_files_diagnostics

    return response


@dataclass
class LintActionExecInfo:
    runner: runner_info.ExtensionRunnerInfo
    action_name: str
    request_data: list[dict[str, str | list[str]]] = field(default_factory=list)


async def workspace_diagnostic(
    ls: LanguageServer, params: types.WorkspaceDiagnosticParams
) -> types.WorkspaceDiagnosticReport | None:
    # TODO: partial responses
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
        # TODO: support LSP endpoints?
        if "lint_many" in actions_names:
            action_name = "lint_many"
        elif "lint" in actions_names:
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
        if exec_info.action_name == "lint_many":
            exec_info.request_data = [
                {"file_paths": [file_path.as_posix() for file_path in files_for_runner]}
            ]
        elif exec_info.action_name == "lint":
            exec_info.request_data = [
                {"file_path": file_path.as_posix()} for file_path in files_for_runner
            ]

    exec_infos = list(exec_info_by_project_dir_path.values())
    send_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for exec_info in exec_infos:
                for request_data in exec_info.request_data:
                    task = tg.create_task(
                        runner_client.run_action(
                            runner=exec_info.runner,
                            action_name=exec_info.action_name,
                            params=[request_data],
                        )
                    )
                    send_tasks.append(task)
    except ExceptionGroup as eg:
        logger.error(f"Error while sending opened document: {eg.exceptions}")

    responses = [task.result() for task in send_tasks]

    items: list[types.WorkspaceDocumentDiagnosticReport] = []
    for response in responses:
        if response is None:
            continue
        else:
            for file_path_str, lint_messages in response.get("messages", {}).items():
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
