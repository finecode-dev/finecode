from dataclasses import dataclass
from pathlib import Path

import finecode.domain as domain


@dataclass
class RunnerContext:
    project_dir_path: Path
    project: domain.Project
