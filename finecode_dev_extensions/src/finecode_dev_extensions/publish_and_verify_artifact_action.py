import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class PublishAndVerifyArtifactRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri
    dist_artifact_paths: list[ResourceUri]
    force: bool = False


class PublishAndVerifyArtifactRunContext(
    code_action.RunActionContext[PublishAndVerifyArtifactRunPayload]
):
    pass


@dataclasses.dataclass
class PublishAndVerifyArtifactRunResult(code_action.RunActionResult):
    version: str
    published_registries: list[str]
    """Registries that accepted the upload. A registry that was already up to
    date is absent from this and from both error maps."""
    verification_errors: dict[str, list[str]]  # registry_name -> errors
    publish_errors: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    """registry_name -> errors, for registries whose *upload* failed. Disjoint
    from `published_registries`: an upload failure means nothing was verified
    there either."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PublishAndVerifyArtifactRunResult):
            return
        self.version = other.version
        self.published_registries = other.published_registries
        self.verification_errors = other.verification_errors
        self.publish_errors = other.publish_errors

    def to_text(self) -> str | textstyler.StyledText:
        lines = []
        if self.published_registries:
            lines.append(
                f"Published version {self.version} to: {', '.join(self.published_registries)}"
            )
        elif not self.publish_errors:
            lines.append(f"Version {self.version} was already published")

        if self.publish_errors:
            lines.append("Publish errors:")
            for registry, errors in self.publish_errors.items():
                for error in errors:
                    lines.append(f"  - {registry}: {error}")

        if self.verification_errors:
            lines.append("Verification errors:")
            for registry, errors in self.verification_errors.items():
                for error in errors:
                    lines.append(f"  - {registry}: {error}")
        elif self.published_registries:
            lines.append("Verification successful for all published registries")

        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.verification_errors or self.publish_errors:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PublishAndVerifyArtifactAction(
    code_action.Action[
        PublishAndVerifyArtifactRunPayload,
        PublishAndVerifyArtifactRunContext,
        PublishAndVerifyArtifactRunResult,
    ]
):
    DESCRIPTION = (
        "Publish a distribution artifact and verify it is available in registries."
    )
    PAYLOAD_TYPE = PublishAndVerifyArtifactRunPayload
    RUN_CONTEXT_TYPE = PublishAndVerifyArtifactRunContext
    RESULT_TYPE = PublishAndVerifyArtifactRunResult
