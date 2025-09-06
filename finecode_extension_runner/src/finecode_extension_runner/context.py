from __future__ import annotations

from dataclasses import dataclass, field
from finecode_extension_runner import domain


@dataclass
class RunnerContext:
    project: domain.Project
    action_cache_by_name: dict[str, domain.ActionCache] = field(default_factory=dict)
    # don't overwrite, only append and remove
    docs_owned_by_client: list[str] = field(default_factory=list)
