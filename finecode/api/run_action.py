import importlib
from pathlib import Path

from loguru import logger

from finecode.api import run_utils
import finecode.domain as domain
import finecode.workspace_context as workspace_context
from .collect_actions import collect_actions


def run(
    action: str,
    apply_on: Path,
    project_root: Path,
    ws_context: workspace_context.WorkspaceContext,
) -> None:
    root_actions, all_actions = collect_actions(project_root, ws_context=ws_context)
    if action not in root_actions:
        logger.warning(
            f"Action {action} not found. Available actions: {','.join(root_actions)}"
        )
        return

    __run_action(all_actions[action], apply_on, all_actions, project_root=project_root)


def __run_action(
    action: domain.Action,
    apply_on: Path,
    all_actions: dict[str, domain.Action],
    project_root: Path,
) -> None:
    current_venv_path = run_utils.get_current_venv_path()
    try:
        project_venv_path = run_utils.get_project_venv_path(project_root)
    except run_utils.VenvNotFound:
        return

    if current_venv_path != project_venv_path:
        # TODO: check that project is managed via poetry
        exit_code, output = run_utils.run_cmd_in_dir(
            f"poetry run python -m finecode.cli run {action.name} {apply_on.as_posix()}",
            project_root,
        )
        logger.debug(f"Output: {output}")
        if exit_code != 0:
            logger.error(f"Action execution failed: {output}")
        else:
            logger.success(f"Action {action.name} successfully executed")
        return

    if len(action.subactions) > 0:
        # TODO: handle circular deps
        for subaction in action.subactions:
            try:
                subaction = all_actions[subaction]
            except KeyError:
                raise Exception(f"Action {subaction} not found")
            __run_action(subaction, apply_on, all_actions, project_root=project_root)
    elif action.source is not None:
        logger.debug(f"Run {action.name} on {str(apply_on.absolute())}")
        try:
            action_cls = run_utils.import_class_by_source_str(action.source)
        except ModuleNotFoundError:
            return
        # TODO: collect config
        config = {}
        action_instance = action_cls(config=config)
        try:
            action_instance.run(apply_on)
        except Exception as e:
            logger.exception(e)
            # TODO: exit code != 0
    else:
        logger.warning(
            f"Action {action.name} has neither source nor subactions, skip it"
        )
