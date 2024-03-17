from pathlib import Path
from typing import Any

from loguru import logger

import finecode.config_models as config_models
import finecode.domain as domain
import finecode.workspace_context as workspace_context


def collect_actions_recursively(
    root_dir: Path, ws_context: workspace_context.WorkspaceContext
) -> domain.Package:
    try:
        root_package = ws_context.ws_packages[root_dir]
    except KeyError:
        raise Exception('Root package not found')

    return root_package

    # root_package = domain.Package(name=root_dir.name, path=root_dir)
    # def_files_generator = root_dir.rglob("*")
    # for def_file in def_files_generator:
    #     if def_file.name not in {"pyproject.toml", "package.json", "finecode.toml"}:
    #         continue

    #     if not finecode_is_enabled_in_def(def_file):
    #         continue

    #     path_parts = def_file.parent.relative_to(root_dir).parts
    #     current_package = root_package
    #     for part in path_parts:
    #         try:
    #             current_package = next(
    #                 package
    #                 for package in current_package.subpackages
    #                 if package.name == part
    #             )
    #         except StopIteration:
    #             new_package = domain.Package(
    #                 name=part, path=current_package.path / part
    #             )
    #             current_package.subpackages.append(new_package)
    #             current_package = new_package
    #
    #     root_actions, all_actions = collect_actions(def_file, ws_context=ws_context)
    #     for action_name in root_actions:
    #         try:
    #             action_info = all_actions[action_name]
    #             current_package.actions.append(action_info)
    #         except KeyError:
    #             # TODO: process correctly, return as invalid
    #             logger.warning(f"Action not found: {action_name}")
    # return root_package


def collect_actions(
    package_path: Path, ws_context: workspace_context.WorkspaceContext
) -> tuple[domain.RootActions, domain.AllActions]:
    if package_path.as_posix() in ws_context.actions_by_package_path:
        logger.trace(f"Found actions for {package_path.as_posix()} in context")
        return ws_context.actions_by_package_path[package_path.as_posix()]

    try:
        config = ws_context.ws_packages_raw_configs[package_path]
    except KeyError:
        return ([], {})
    result = _collect_actions_in_config(config)


    ws_context.actions_by_package_path[package_path.as_posix()] = result
    return result


def _collect_actions_in_config(
    config: dict[str, Any]
) -> tuple[domain.RootActions, domain.AllActions]:
    root_actions: domain.RootActions = []
    all_actions: domain.AllActions = {}
    try:
        finecode_config = config_models.FinecodeConfig(**config["tool"]["finecode"])
    # TODO: handle validation error
    except KeyError:
        return (root_actions, all_actions)

    for action_name, action_def_raw in (
        config["tool"]["finecode"].get("action", {}).items()
    ):
        # TODO: handle validation errors
        action_def = config_models.ActionConfig(**action_def_raw)
        subactions: list[str] = []
        for subaction in action_def.subactions:
            subactions.append(subaction.name)
            if subaction.source is not None:
                all_actions[subaction.name] = domain.Action(
                    name=subaction.name, source=subaction.source
                )
        all_actions[action_name] = domain.Action(
            name=action_name, subactions=subactions, source=action_def.source
        )

    for root_action in finecode_config.actions:
        root_actions.append(root_action.name)
        if root_action.source is not None:
            source = root_action.source
            subactions = []
            all_actions[root_action.name] = domain.Action(
                name=root_action.name, subactions=subactions, source=source
            )
        else:
            if root_action.name not in all_actions:
                raise Exception(
                    f"Action {root_action.name} has neither source or definition with subactions"
                )

    return (root_actions, all_actions)
