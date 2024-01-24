from dataclasses import dataclass, field
from pathlib import Path

import finecode.domain as domain


@dataclass
class WorkspaceContext:
    ws_dir_path: Path

    # cache
    # actions_by_package_path: dict[str, domain.Action] = field(default_factory=dict)
    actions_by_package_path: dict[
        str, tuple[domain.RootActions, domain.AllActions]
    ] = field(default_factory=dict)
    # <directory: <action_name: package_path>>
    package_path_by_dir_and_action: dict[str, dict[str, Path]] = field(
        default_factory=dict
    )
