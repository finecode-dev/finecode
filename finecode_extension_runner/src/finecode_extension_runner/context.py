from __future__ import annotations

from dataclasses import dataclass, field
from finecode_extension_runner import domain, er_wal
from finecode_extension_runner.di.registry import Registry
from finecode_extension_api import service


@dataclass
class RunnerContext:
    project: domain.Project
    di_registry: Registry = field(default_factory=Registry)
    action_cache_by_name: dict[str, domain.ActionCache] = field(default_factory=dict)
    project_config_version: int = 0
    running_services: dict[service.Service, domain.RunningServiceInfo] = field(
        default_factory=dict
    )
    wal_writer: er_wal.ErWalWriter | None = None
