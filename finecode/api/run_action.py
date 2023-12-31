import os
import importlib
from pathlib import Path

from command_runner import command_runner
from loguru import logger

import finecode.domain as domain
from .collect_actions import collect_actions


def run(action: str, apply_on: Path, project_root: Path) -> None:
    # TODO: find def file instead of hardcoded pyproject.toml
    root_actions, all_actions = collect_actions(project_root / "pyproject.toml")
    if action not in root_actions:
        logger.warning(
            f"Action {action} not found. Available actions: {','.join(root_actions)}"
        )
        return

    current_project_path = Path(__file__).parent.parent.parent
    if current_project_path == project_root:
        _project_root = None
    else:
        _project_root = project_root

    __run_action(all_actions[action], apply_on, all_actions, project_root=_project_root)


def __run_action(
    action: domain.Action,
    apply_on: Path,
    all_actions: dict[str, domain.Action],
    project_root: Path | None = None,
) -> None:
    if project_root is not None:
        # TODO: check that project is managed via poetry
        old_current_dir = os.getcwd()
        os.chdir(project_root)
        exit_code, output = command_runner(
            f"poetry run python -m finecode.cli run {action.name} {apply_on.as_posix()}"
        )
        os.chdir(old_current_dir)
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
        cls_name = action.source.split(".")[-1]
        module_path = ".".join(action.source.split(".")[:-1])
        logger.debug(f"Run {action.name} on {str(apply_on.absolute())}")

        # TODO: handle errors
        module = importlib.import_module(module_path)
        try:
            action_cls = module.__dict__[cls_name]
        except KeyError:
            logger.error(f"Class {cls_name} not found in action module {module_path}")
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
