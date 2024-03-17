from pathlib import Path
from typing import Any

from loguru import logger

import finecode.domain as domain
from finecode import config_models, workspace_context, views
from finecode.api import run_utils


def collect_views_in_packages(
    packages: list[domain.Package], ws_context: workspace_context.WorkspaceContext
) -> dict[Path, list[domain.View]]:
    result: dict[Path, list[domain.View]] = {}

    for package in packages:
        package_config = ws_context.ws_packages_raw_configs.get(package.path, None)
        if package_config:
            result[package.path] = _collect_views_in_config(package_config)
            package.views = result[package.path]
    return result


def _collect_views_in_config(config: dict[str, Any]) -> list[domain.View]:
    try:
        finecode_config = config_models.FinecodeConfig(**config["tool"]["finecode"])
    # TODO: handle validation error
    except KeyError:
        return []

    result: list[domain.View] = []
    for view_def in finecode_config.views:
        result.append(domain.View(name=view_def.name, source=view_def.source))

    return result


def show_view(
    view_name: str, package_path: Path, ws_context: workspace_context.WorkspaceContext
) -> list[views.BaseEntity]:
    current_venv_path = run_utils.get_current_venv_path()
    try:
        project_venv_path = run_utils.get_project_venv_path(package_path)
    except run_utils.VenvNotFound:
        return []

    if current_venv_path != project_venv_path:
        # TODO: check that project is managed via poetry
        exit_code, output = run_utils.run_cmd_in_dir(f'', package_path)
        logger.debug(f"Output: {output}")
        if exit_code != 0:
            logger.error(f"View show failed: {output}")
            return []

    try:
        view = next(view for view in ws_context.ws_packages[package_path].views if view.name == view_name)
    except (KeyError, StopIteration):
        raise Exception("Package or view not found")
    
    try:
        view_cls = run_utils.import_class_by_source_str(view.source)
    except ModuleNotFoundError:
        raise Exception("Cannot import view source")
    assert isinstance(view_cls, views.BaseView)
    try:
        root_entity_manager_cls = view_cls.MANAGERS[view_cls.ROOT_ENTITY]
    except KeyError:
        raise Exception("No manager for root entity")

    root_entity_manager = root_entity_manager_cls()
    els = root_entity_manager.get_list(parent=None, ws_context=ws_context)
    return els
