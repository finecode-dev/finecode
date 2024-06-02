from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from finecode.api import run_utils
import finecode.domain as domain

if TYPE_CHECKING:
    from finecode.workspace_manager.main import ExtensionRunnerInfo


@dataclass
class WorkspaceContext:
    # ws directories paths - expected to be workspace root and other directories in workspace if
    # they are outside of workspace root
    ws_dirs_paths: list[Path]
    # tree of packages for each path in ws_dirs_pathes
    ws_packages: dict[Path, domain.Package] = field(default_factory=dict)
    # <package_path:config>
    ws_packages_raw_configs: dict[Path, dict[str, Any]] = field(default_factory=dict)
    ws_packages_extension_runners: dict[Path, ExtensionRunnerInfo] = field(
        default_factory=dict
    )

    # cache
    # <directory: <action_name: package_path>>
    package_path_by_dir_and_action: dict[str, dict[str, Path]] = field(
        default_factory=dict
    )
    current_venv_path: Path = field(
        default_factory=lambda: run_utils.get_current_venv_path()
    )
    venv_path_by_package_path: dict[Path, Path] = field(default_factory=dict)
