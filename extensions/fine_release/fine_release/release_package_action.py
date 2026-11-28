# docs: docs/reference/actions.md
from __future__ import annotations

import dataclasses
import enum

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
    """At least one registry failed (some others may have PUBLISHED), or the
    release failed before any registry outcome was determined (see error)."""
    BLOCKED = "blocked"
    """A transitive same-run dependency FAILED, so not attempted. Produced only
    by the workspace orchestrator (ADR-0065) — release_package never emits it,
    because only the orchestrator knows the dependency graph."""


@dataclasses.dataclass
class ReleasePackageRunPayload(code_action.RunActionPayload):
    package_name: str
    version: str
    src_artifact_def_path: ResourceUri | None = None
    """None -> the receiving project's own artifact definition."""
    dry_run: bool = False
    """Preview mode: resolve registries and report what would happen, without
    building, publishing or tagging."""


@dataclasses.dataclass
class ReleasePackageState:
    registries: list[str] = dataclasses.field(default_factory=list)
    """Resolved registry names, in configuration order."""
    build_output_paths: list[ResourceUri] = dataclasses.field(default_factory=list)
    registry_results: list[RegistryPublishResult] = dataclasses.field(
        default_factory=list
    )
    created_refs: list[str] = dataclasses.field(default_factory=list)
    """Refs created for this package; the orchestrator publishes them."""
    error: str | None = None
    """Set by the handler that failed. Every later handler in the chain returns
    early while this is set, so a failed build is never published or tagged."""


class ReleasePackageRunContext(code_action.RunActionContext[ReleasePackageRunPayload]):
    STATE_TYPE = ReleasePackageState
    state: ReleasePackageState


@dataclasses.dataclass
class ReleasePackageRunResult(code_action.RunActionResult):
    package_name: str = ""
    version: str = ""
    src_artifact_def_path: ResourceUri | None = None
    outcome: PackageReleaseOutcome = PackageReleaseOutcome.FAILED
    registries: list[RegistryPublishResult] = dataclasses.field(default_factory=list)
    """Empty when the release failed before any registry outcome was determined
    — outcomes are never fabricated for registries nothing was attempted
    against; `error` explains what happened instead."""
    created_refs: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None
    """Non-registry failure only (e.g. build, registry resolution, a publish that
    raised before dispatch); registry errors live in registries[].errors."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ReleasePackageRunResult):
            return

        self.package_name = other.package_name
        self.version = other.version
        self.src_artifact_def_path = other.src_artifact_def_path
        self.outcome = other.outcome
        self.registries = other.registries
        self.created_refs = other.created_refs
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        lines = [f"{self.package_name} {self.version}: {self.outcome}"]
        if self.error is not None:
            lines.append(f"  {self.error}")
        for registry in self.registries:
            lines.append(f"  {registry.registry}: {registry.outcome}")
            for error in registry.errors:
                lines.append(f"    - {error}")
        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.outcome == PackageReleaseOutcome.FAILED:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


def derive_package_outcome(
    registries: list[RegistryPublishResult],
) -> PackageReleaseOutcome:
    # An empty list means no registry outcome was ever determined — either no
    # configured registry resolved, or the release failed before dispatch. Both
    # set state.error, which result_from_state checks first, so reaching FAILED
    # here without an error set is a defensive fallback.
    if not registries:
        return PackageReleaseOutcome.FAILED
    if any(r.outcome == RegistryPublishOutcome.FAILED for r in registries):
        return PackageReleaseOutcome.FAILED
    if any(r.outcome == RegistryPublishOutcome.PUBLISHED for r in registries):
        return PackageReleaseOutcome.PUBLISHED
    if any(r.outcome == RegistryPublishOutcome.WOULD_PUBLISH for r in registries):
        return PackageReleaseOutcome.WOULD_PUBLISH
    return PackageReleaseOutcome.SKIPPED


def result_from_state(
    payload: ReleasePackageRunPayload, state: ReleasePackageState
) -> ReleasePackageRunResult:
    """Build the action result from accumulated state.

    Every handler returns this on every path, so the action always produces a
    result (R-308) regardless of which step ended the chain.
    """
    outcome = (
        PackageReleaseOutcome.FAILED
        if state.error is not None
        else derive_package_outcome(state.registry_results)
    )
    return ReleasePackageRunResult(
        package_name=payload.package_name,
        version=payload.version,
        src_artifact_def_path=payload.src_artifact_def_path,
        outcome=outcome,
        registries=list(state.registry_results),
        created_refs=list(state.created_refs),
        error=state.error,
    )


class ReleasePackageAction(
    code_action.Action[
        ReleasePackageRunPayload,
        ReleasePackageRunContext,
        ReleasePackageRunResult,
    ]
):
    """Release one package: build it, publish and verify it to every configured
    registry, and record the publish as a git tag (ADR-0065).

    The chain is the *package's own* — a package configures, replaces or drops
    steps by editing its handler list, without touching the workspace release.

    Handlers record failures in `run_context.state.error` instead of raising, so
    a failed release comes back as a FAILED result carrying whatever context the
    failing step had. Every handler must therefore return early while `error` is
    set — otherwise a failed build would still be published or tagged.

    Refs are created here but never pushed: pushing acts on the one repository
    shared by every package, so the workspace release publishes all refs a run
    produced in a single operation (ADR-0065)."""

    DESCRIPTION = (
        "Release a single package: build, publish and verify it to its "
        "registries, and record the publish as a git tag."
    )
    HANDLER_EXECUTION = code_action.HandlerExecution.SEQUENTIAL
    PAYLOAD_TYPE = ReleasePackageRunPayload
    RUN_CONTEXT_TYPE = ReleasePackageRunContext
    RESULT_TYPE = ReleasePackageRunResult
