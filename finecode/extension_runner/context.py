from dataclasses import dataclass

import finecode.extension_runner.domain as domain


@dataclass
class RunnerContext:
    project: domain.Project
