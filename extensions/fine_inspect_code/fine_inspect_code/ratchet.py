from __future__ import annotations

import collections.abc
import dataclasses
import pathlib
import typing

from finecode_extension_api import service
from finecode_extension_api.code_action import ItemCoverage
from finecode_extension_api.resource_uri import ResourceUri

from fine_inspect_code.diagnostic_types import Diagnostic, RatchetTarget
from fine_inspect_code.ratchet_matching import RatchetOutcome, RatchetScope

__all__ = ["DiagnosticsRatchetSettings", "IDiagnosticsRatchet", "RatchetTarget"]


@dataclasses.dataclass
class DiagnosticsRatchetSettings:
    snapshot_file: str = "finecode-diagnostics-baseline.json"


class IDiagnosticsRatchet(service.Service, typing.Protocol):
    def snapshot_path(self, project_dir: pathlib.Path) -> pathlib.Path: ...

    async def apply(
        self,
        *,
        contributor: str,
        checked_project: pathlib.Path,
        messages: dict[ResourceUri, list[Diagnostic]],
        scope: RatchetScope,
        unhandled: collections.abc.Sequence[ItemCoverage],
    ) -> RatchetOutcome: ...
