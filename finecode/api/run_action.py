import asyncio
from pathlib import Path

from loguru import logger

import finecode.domain as domain
import finecode.workspace_context as workspace_context
import finecode.workspace_manager as workspace_manager
from finecode.api import run_utils

from .collect_actions import collect_actions, get_subaction


def run(
    action: str,
    apply_on: Path,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> None:
    # TODO: optimize: find action info and collect all only if looked one was not found
    actions = collect_actions(project_root, ws_context=ws_context)
    try:
        action_obj = next(action_obj for action_obj in actions if action_obj.name == action)
    except StopIteration:
        logger.warning(
            f"Action {action} not found. Available actions: {','.join([action_obj.name for action_obj in actions])}"
        )
        return

    __run_action(
        action_obj,
        apply_on,
        project_root=project_root,
        ws_context=ws_context,
    )


def __run_action(
    action: domain.Action,
    apply_on: Path,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> None:
    logger.trace(f"Execute action {action.name} on {apply_on}")
    try:
        project_venv_path = ws_context.venv_path_by_package_path[project_root]
    except KeyError:
        logger.error(f"Project has no venv path: {project_root}")
        return

    try:
        project_package = ws_context.ws_packages[project_root]
    except KeyError:
        logger.error(f"Project package not found: {project_root}")
        return

    if project_package.actions is None:
        logger.error("Project actions are not read yet")
        return

    # check first project package, then workspace package
    current_venv_is_project_venv = ws_context.current_venv_path == project_venv_path
    current_venv_is_workspace_venv = not current_venv_is_project_venv
    action_in_project = False
    try:
        next(a for a in project_package.actions if a.name == action.name)
        action_in_project = True
    except StopIteration:
        action_found = False
        if current_venv_is_workspace_venv:
            try:
                workspace_package = ws_context.ws_packages[project_root]
            except KeyError:
                logger.error(f"Workspace package not found: {project_root}")
                return

            if workspace_package.actions is None:
                logger.error("Actions in workspace package are not read yet")
                return

            try:
                next(a for a in workspace_package.actions if a.name == action.name)
                action_found = True
            except StopIteration:
                ...
        if not action_found:
            logger.error(f"Action {action.name} not found neither in project nor in workspace")
            return

    ws_context.ignore_watch_paths.add(apply_on)
    if action_in_project and current_venv_is_workspace_venv:
        if project_root in ws_context.ws_packages_extension_runners:
            # extension runner is running for this project, send command to it
            asyncio.run(
                workspace_manager.run_action_in_runner(
                    runner=ws_context.ws_packages_extension_runners[project_root],
                    action=action,
                    apply_on=apply_on,
                )
            )
        else:
            # no extension runner, use CLI

            # TODO: check that project is managed via poetry
            exit_code, output = run_utils.run_cmd_in_dir(
                f"poetry run python -m finecode.cli action run {action.name} {apply_on.absolute().as_posix()}",
                dir_path=project_root,
            )
            logger.debug(f"Output: {output}")
            if exit_code != 0:
                logger.error(f"Action execution failed: {output}")
            else:
                logger.success(f"Action {action.name} successfully executed")
    else:
        # run in current env
        if len(action.subactions) > 0:
            # TODO: handle circular deps
            for subaction in action.subactions:
                try:
                    subaction_obj = get_subaction(
                        name=subaction, package_path=project_root, ws_context=ws_context
                    )
                except ValueError:
                    raise Exception(f"Action {subaction} not found")
                __run_action(
                    subaction_obj,
                    apply_on,
                    project_root=project_root,
                    ws_context=ws_context,
                )
        elif action.source is not None:
            logger.debug(f"Run {action.name} on {str(apply_on.absolute())}")
            try:
                action_cls = run_utils.import_class_by_source_str(action.source)
                action_config_cls = run_utils.import_class_by_source_str(action.source + "Config")
            except ModuleNotFoundError:
                ws_context.ignore_watch_paths.remove(apply_on)
                return

            try:
                action_config = ws_context.ws_packages[project_root].actions_configs[action.name]
            except KeyError:
                action_config = {}

            config = action_config_cls(**action_config)
            action_instance = action_cls(config=config)
            try:
                action_instance.run(apply_on)
            except Exception as e:
                logger.exception(e)
                # TODO: exit code != 0
        else:
            logger.warning(f"Action {action.name} has neither source nor subactions, skip it")
            return
        ws_context.ignore_watch_paths.remove(apply_on)
        logger.trace(f"End of execution of action {action.name} on {apply_on}")
