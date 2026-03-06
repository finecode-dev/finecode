from __future__ import annotations

from dataclasses import dataclass, field
from finecode_extension_runner import domain
from finecode_extension_api import service


@dataclass
class RunnerContext:
    project: domain.Project
    action_cache_by_name: dict[str, domain.ActionCache] = field(default_factory=dict)
    project_config_version: int = 0
    running_services: dict[service.Service, domain.RunningServiceInfo] = field(
        default_factory=dict
    )
