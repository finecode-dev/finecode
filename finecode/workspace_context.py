from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import finecode.domain as domain


@dataclass
class WorkspaceContext:
    ws_dirs_pathes: list[Path]
    # tree of packages for each path in ws_dirs_pathes
    ws_packages: dict[Path, domain.Package] = field(default_factory=dict)
    # <package_path:config>
    ws_packages_raw_configs: dict[Path, dict[str, Any]] = field(default_factory=dict)

    # cache
    actions_by_package_path: dict[
        str, tuple[domain.RootActions, domain.AllActions]
    ] = field(default_factory=dict)
    # <directory: <action_name: package_path>>
    package_path_by_dir_and_action: dict[str, dict[str, Path]] = field(
        default_factory=dict
    )
