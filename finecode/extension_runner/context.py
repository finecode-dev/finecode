from dataclasses import dataclass, field

import finecode.extension_runner.domain as domain
from finecode import CodeAction


@dataclass
class RunnerContext:
    project: domain.Project
    actions_instances_by_name: dict[str, CodeAction] = field(default_factory=dict)
    # don't overwrite, only append and remove
    docs_owned_by_client: list[str] = field(default_factory=list)
