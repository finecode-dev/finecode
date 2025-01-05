from __future__ import annotations

import site
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import finecode.workspace_manager.domain as domain

if TYPE_CHECKING:
    from finecode.workspace_manager.runner.runner_info import ExtensionRunnerInfo


@dataclass
class WorkspaceContext:
    # ws directories paths - expected to be workspace root and other directories in workspace if
    # they are outside of workspace root
    ws_dirs_paths: list[Path]
    # tree of projects for each path in ws_dirs_pathes
    ws_projects: dict[Path, domain.Project] = field(default_factory=dict)
    # <project_path:config>
    ws_projects_raw_configs: dict[Path, dict[str, Any]] = field(default_factory=dict)
    ws_projects_extension_runners: dict[Path, ExtensionRunnerInfo] = field(default_factory=dict)
    ignore_watch_paths: set[Path] = field(default_factory=set)

    # cache
    # <directory: <action_name: project_path>>
    project_path_by_dir_and_action: dict[str, dict[str, Path]] = field(default_factory=dict)
    current_venv_path: Path = field(default_factory=lambda: get_current_venv_path())
    cached_actions_by_id: dict[str, CachedAction] = field(default_factory=dict)


@dataclass
class CachedAction:
    action_id: str
    project_path: Path
    action_name: str


def get_current_venv_path() -> Path:
    return Path(site.getsitepackages()[0]).parent.parent.parent
