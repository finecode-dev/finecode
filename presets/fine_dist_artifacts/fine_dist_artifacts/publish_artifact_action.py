# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class PublishArtifactRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri
    dist_artifact_paths: list[ResourceUri]
    force: bool = False


class PublishArtifactRunContext(
    code_action.RunActionContext[PublishArtifactRunPayload]
): ...


@dataclasses.dataclass
class PublishArtifactRunResult(code_action.RunActionResult):
    version: str
    published_registries: list[str]
    """Registries that accepted the upload. A registry that was already up to
    date is absent from both this and `failed_registries`."""
    failed_registries: dict[str, str] = dataclasses.field(default_factory=dict)
    """registry_name -> error, for registries whose upload failed. One registry
    failing does not prevent the others from publishing."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PublishArtifactRunResult):
            return

        self.version = other.version
        self.published_registries = other.published_registries
        self.failed_registries = other.failed_registries

    def to_text(self) -> str | textstyler.StyledText:
        lines: list[str] = []
        if self.published_registries:
            registries_str = ", ".join(self.published_registries)
            lines.append(f"Published version {self.version} to: {registries_str}")
        elif not self.failed_registries:
            lines.append(f"Version {self.version} is already published")

        for registry_name, error in self.failed_registries.items():
            lines.append(f"Failed to publish to {registry_name}: {error}")

        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.failed_registries:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PublishArtifactAction(
    code_action.Action[
        PublishArtifactRunPayload,
        PublishArtifactRunContext,
        PublishArtifactRunResult,
    ]
):
    """Publish a distribution artifact to all configured registries."""

    DESCRIPTION = "Publish a distribution artifact to all configured registries."
    PAYLOAD_TYPE = PublishArtifactRunPayload
    RUN_CONTEXT_TYPE = PublishArtifactRunContext
    RESULT_TYPE = PublishArtifactRunResult
