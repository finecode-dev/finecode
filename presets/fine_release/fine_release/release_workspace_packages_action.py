# docs: docs/reference/actions.md
from __future__ import annotations

import dataclasses
import pathlib

# Outcome vocabulary is owned by the per-package action, which is what observes
# registry outcomes; only BLOCKED is produced here (ADR-0065).
from fine_release.release_package_action import (
    PackageReleaseOutcome,
    RegistryPublishResult,
)
from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class PackageReleaseResult:
    package_name: str
    src_artifact_def_path: ResourceUri
    version: str
    outcome: PackageReleaseOutcome
    registries: list[RegistryPublishResult] = dataclasses.field(default_factory=list)
    """Empty when BLOCKED, and whenever the package release ended before any
    registry outcome was determined (FAILED, see error)."""
    error: str | None = None
    """Non-registry failure only (build, registry resolution, a publish that
    raised before dispatch, or a package release that could not be run at all);
    registry errors live in registries[].errors."""


@dataclasses.dataclass
class ReleaseWorkspacePackagesRunPayload(code_action.RunActionPayload):
    dry_run: bool = False
    project_paths: list[ResourceUri] | None = None
    """None = whole workspace; else restrict the candidate set."""


@dataclasses.dataclass
class _Candidate:
    project_path: pathlib.Path
    package_name: str
    version: str
    src_artifact_def_path: ResourceUri


@dataclasses.dataclass
class ReleaseWorkspacePackagesState:
    candidates_by_name: dict[str, _Candidate] = dataclasses.field(default_factory=dict)
    ordered_names: list[str] = dataclasses.field(default_factory=list)
    dependencies_by_name: dict[str, set[str]] = dataclasses.field(default_factory=dict)


class ReleaseWorkspacePackagesRunContext(
    code_action.RunActionContext[ReleaseWorkspacePackagesRunPayload]
):
    STATE_TYPE = ReleaseWorkspacePackagesState
    state: ReleaseWorkspacePackagesState


@dataclasses.dataclass
class ReleaseWorkspacePackagesRunResult(code_action.RunActionResult):
    dry_run: bool = False
    packages: list[PackageReleaseResult] = dataclasses.field(default_factory=list)
    """In the dependency order used."""
    error: str | None = None
    """A run-level failure not attributable to a single package — a handler that
    could not complete a repository-wide step (for example publishing the refs a
    run produced). Fails the run (non-zero return code) without being tied to any
    one package's outcome. Per-package failures are carried by each package's own
    `outcome`, not here."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ReleaseWorkspacePackagesRunResult):
            return

        self.dry_run = other.dry_run
        self.packages = other.packages
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        lines = [f"{p.package_name} {p.version}: {p.outcome}" for p in self.packages]
        if self.error is not None:
            lines.append(self.error)
        return "\n".join(lines) if lines else "No release candidates"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        run_failed = self.error is not None or any(
            package.outcome == PackageReleaseOutcome.FAILED for package in self.packages
        )
        return (
            code_action.RunReturnCode.ERROR
            if run_failed
            else code_action.RunReturnCode.SUCCESS
        )


class ReleaseWorkspacePackagesAction(
    code_action.Action[
        ReleaseWorkspacePackagesRunPayload,
        ReleaseWorkspacePackagesRunContext,
        ReleaseWorkspacePackagesRunResult,
    ]
):
    """Release every workspace package whose declared version is absent from
    its registry, in dependency order. A failed publish blocks only its
    transitive dependents (ADR-0062).

    This action owns only what is inherently cross-package: candidate
    discovery, dependency ordering, dependent blocking, and publication of the
    refs a run produced. Building, publishing and tagging one package belong to
    that package's own `release_package` chain, which this action delegates to
    once per candidate (ADR-0065)."""

    DESCRIPTION = (
        "Release every workspace package whose declared version is absent "
        "from its registry, in dependency order."
    )
    SCOPE = code_action.ActionScope.WORKSPACE
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
    PAYLOAD_TYPE = ReleaseWorkspacePackagesRunPayload
    RUN_CONTEXT_TYPE = ReleaseWorkspacePackagesRunContext
    RESULT_TYPE = ReleaseWorkspacePackagesRunResult
