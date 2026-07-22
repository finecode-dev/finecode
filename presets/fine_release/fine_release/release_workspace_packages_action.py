# docs: docs/reference/actions.md
from __future__ import annotations

import dataclasses
import enum
import pathlib

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


class RegistryPublishOutcome(enum.StrEnum):
    PUBLISHED = "published"
    """Newly published to this registry this run."""
    SKIPPED = "skipped"
    """Already present in this registry."""
    WOULD_PUBLISH = "would_publish"
    """Dry-run only."""
    FAILED = "failed"
    """Publish or verify failed for this registry."""


@dataclasses.dataclass
class RegistryPublishResult:
    registry: str
    outcome: RegistryPublishOutcome
    errors: list[str] = dataclasses.field(default_factory=list)
    """Non-empty only when outcome is FAILED."""


class PackageReleaseOutcome(enum.StrEnum):
    PUBLISHED = "published"
    """Newly published to every registry that needed it."""
    SKIPPED = "skipped"
    """Declared version already present in every registry."""
    WOULD_PUBLISH = "would_publish"
    """Dry-run only: absent from at least one registry."""
    FAILED = "failed"
    """At least one registry failed (some others may have PUBLISHED)."""
    BLOCKED = "blocked"
    """A transitive same-run dependency FAILED, so not attempted."""


@dataclasses.dataclass
class PackageReleaseResult:
    package_name: str
    src_artifact_def_path: ResourceUri
    version: str
    outcome: PackageReleaseOutcome
    registries: list[RegistryPublishResult] = dataclasses.field(default_factory=list)
    """Empty when BLOCKED; when publish_and_verify_artifact raised before
    per-registry dispatch; or when no registries were configured/resolved for
    the package (FAILED, see error)."""
    error: str | None = None
    """Non-registry failure only (build, version read, no registries
    configured); registry errors live in registries[].errors."""


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
    had_failures: bool = False
    """True iff any package FAILED."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ReleaseWorkspacePackagesRunResult):
            return

        self.dry_run = other.dry_run
        self.packages = other.packages
        self.had_failures = other.had_failures

    def to_text(self) -> str | textstyler.StyledText:
        lines = [f"{p.package_name} {p.version}: {p.outcome}" for p in self.packages]
        return "\n".join(lines) if lines else "No release candidates"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.had_failures:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class ReleaseWorkspacePackagesAction(
    code_action.Action[
        ReleaseWorkspacePackagesRunPayload,
        ReleaseWorkspacePackagesRunContext,
        ReleaseWorkspacePackagesRunResult,
    ]
):
    """Release every workspace package whose declared version is absent from
    its registry, in dependency order. A failed publish blocks only its
    transitive dependents (ADR-0062); a successful publish gets a best-effort
    git tag recording it (ADR-0060)."""

    DESCRIPTION = (
        "Release every workspace package whose declared version is absent "
        "from its registry, in dependency order."
    )
    SCOPE = code_action.ActionScope.WORKSPACE
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
    PAYLOAD_TYPE = ReleaseWorkspacePackagesRunPayload
    RUN_CONTEXT_TYPE = ReleaseWorkspacePackagesRunContext
    RESULT_TYPE = ReleaseWorkspacePackagesRunResult
