from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from lsprotocol import types

from finecode import pygls_types_utils
from finecode.workspace_manager import domain
from finecode.workspace_manager.runner import runner_client
from finecode.workspace_manager.server import global_state, proxy_utils

if TYPE_CHECKING:
    from pygls.lsp.server import LanguageServer

    from finecode.workspace_manager.runner import runner_info


def map_lint_message_dict_to_diagnostic(lint_message: dict[str, Any]) -> types.Diagnostic:
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

    # TODO: check whether 'lint' action is available and enabled
    # TODO: file is read from file system. If it was changed and not saved in IDE, changes are ignored.
    #       read file using LSP API
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
        map_lint_message_dict_to_diagnostic(lint_message) for lint_message in requested_file_messages
    ]
    response = types.RelatedFullDocumentDiagnosticReport(items=requested_files_diagnostic_items)

    related_files_diagnostics: dict[str, types.FullDocumentDiagnosticReport] = {}
    for file_path_str, file_lint_messages in lint_messages.items():
        file_report = types.FullDocumentDiagnosticReport(
            items=[map_lint_message_dict_to_diagnostic(lint_message) for lint_message in file_lint_messages]
        )
        related_files_diagnostics[pygls_types_utils.path_to_uri_str(file_path_str)] = file_report
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
    # if both parent and child projects have lint action, exclude files of chid from parent
    # check which runners are active and run in them

    projects = global_state.ws_context.ws_projects
    relevant_projects: dict[Path, domain.Project] = {
        path: project for path, project in projects.items() if project.status != domain.ProjectStatus.NO_FINECODE
    }
    exec_info_by_project_dir_path: dict[Path, LintActionExecInfo] = {}
    # exclude projects without lint action
    for project_dir_path, project_def in relevant_projects.copy().items():
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
        exec_info_by_project_dir_path[project_dir_path] = LintActionExecInfo(runner=runner, action_name=action_name)

    relevant_projects_paths: list[Path] = list(relevant_projects.keys())
    # assign files to projects
    files_by_projects: dict[Path, list[Path]] = get_files_by_projects(projects_dirs_paths=relevant_projects_paths)

    for project_dir_path, files_for_runner in files_by_projects.items():
        project = global_state.ws_context.ws_projects[project_dir_path]
        if project.status != domain.ProjectStatus.RUNNING:
            logger.warning(f"Runner of project {project_dir_path} is not running, lint in it will not be executed")
            continue

        exec_info = exec_info_by_project_dir_path[project_dir_path]
        if exec_info.action_name == "lint_many":
            exec_info.request_data = [{"file_paths": [file_path.as_posix() for file_path in files_for_runner]}]
        elif exec_info.action_name == "lint":
            exec_info.request_data = [{"file_path": file_path.as_posix()} for file_path in files_for_runner]

    exec_infos = list(exec_info_by_project_dir_path.values())
    send_tasks: list[asyncio.Task] = []
    try:
        async with asyncio.TaskGroup() as tg:
            for exec_info in exec_infos:
                for request_data in exec_info.request_data:
                    task = tg.create_task(
                        runner_client.run_action(
                            runner=runner, action_name=exec_info.action_name, params=[request_data]
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
                    items=[map_lint_message_dict_to_diagnostic(lint_message) for lint_message in lint_messages],
                )
                items.append(new_report)

    # lsprotocol allows None as return value, but then vscode throws error 'cannot read items of null'
    # keep empty report instead
    return types.WorkspaceDiagnosticReport(items=items)


def get_files_by_projects(projects_dirs_paths: list[Path]) -> dict[Path, list[Path]]:
    files_by_projects_dirs: dict[Path, list[Path]] = {}

    # logger.trace(f"project defs in {dir_path}: {projects_defs}")
    if len(projects_dirs_paths) == 1:
        project_dir = projects_dirs_paths[0]
        files_by_projects_dirs[project_dir] = [path for path in project_dir.rglob("*.py")]
    else:
        # copy to avoid modifying of argument values
        projects_dirs = projects_dirs_paths.copy()
        # sort by depth so that child items are first
        # default reverse path sorting works so, that child items are before their parents
        projects_dirs.sort(reverse=True)
        for index, project_dir_path in enumerate(projects_dirs):
            files_by_projects_dirs[project_dir_path] = []

            child_project_by_rel_path: dict[Path, Path] = {}
            # find children
            for current_project_dir_path in projects_dirs[:index]:
                if not current_project_dir_path.is_relative_to(project_dir_path):
                    break
                else:
                    rel_to_project_dir_path = current_project_dir_path.relative_to(project_dir_path)
                    child_project_by_rel_path[rel_to_project_dir_path] = current_project_dir_path

            # convert child_project_by_rel_path to tree to be able to check whether directory contains
            # subrojects without reiterating
            child_project_tree: dict[str, str] = {}
            for child_rel_path in child_project_by_rel_path.keys():
                current_tree_branch = child_project_tree
                for part in child_rel_path.parts:
                    if part not in current_tree_branch:
                        current_tree_branch[part] = {}
                    current_tree_branch = current_tree_branch[part]

            # use set, because one dir item can have multiple subprojects and we need it only once
            dir_items_with_children: set[str] = set(
                [dir_item_path.parts[0] for dir_item_path in child_project_by_rel_path.keys()]
            )
            if len(dir_items_with_children) == 0:
                # if there are no children with subprojects, we can just rglob
                files_by_projects_dirs[project_dir_path].extend(path for path in project_dir_path.rglob("*.py"))
            else:
                # process all dir items which don't have child projects
                for dir_item in project_dir_path.iterdir():
                    if dir_item.name in dir_items_with_children:
                        continue
                    else:
                        if dir_item.suffix == ".py":
                            files_by_projects_dirs[project_dir_path].append(dir_item)
                        elif dir_item.is_dir():
                            files_by_projects_dirs[project_dir_path].extend(path for path in dir_item.rglob("*.py"))

                # process all dir items which have child projects
                for rel_path in child_project_by_rel_path.keys():
                    rel_path_parts = rel_path.parts
                    current_tree_branch = child_project_tree
                    # iterate from second item because the first one is directory we currently processing
                    for index in range(len(rel_path_parts[1:])):
                        current_path = project_dir_path / "/".join(rel_path_parts[: index + 1])
                        current_tree_branch = current_tree_branch[rel_path_parts[index]]
                        for dir_item in current_path.iterdir():
                            if dir_item.suffix == ".py":
                                files_by_projects_dirs[project_dir_path].append(dir_item)
                            elif dir_item.is_dir():
                                if dir_item.name in current_tree_branch:
                                    # it's a path to child project, skip it
                                    continue
                                else:
                                    # subdirectory without child projects, rglob it
                                    files_by_projects_dirs[project_dir_path].extend(
                                        path for path in dir_item.rglob("*.py")
                                    )

    return files_by_projects_dirs
