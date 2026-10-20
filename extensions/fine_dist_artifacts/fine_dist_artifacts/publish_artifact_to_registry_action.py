# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class PublishArtifactToRegistryRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri
    dist_artifact_paths: list[ResourceUri]
    registry_name: str
    force: bool = False


class PublishArtifactToRegistryRunContext(
    code_action.RunActionContext[PublishArtifactToRegistryRunPayload]
): ...


@dataclasses.dataclass
class PublishArtifactToRegistryRunResult(code_action.RunActionResult):
    registry_name: str
    published_paths: list[ResourceUri]
    error: str | None = None
    """Why this registry rejected the upload. A publish failure is reported here
    rather than raised, so that a caller publishing to several registries can
    attribute the failure to one of them and still report the others' outcomes.
    `published_paths` is empty whenever this is set."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PublishArtifactToRegistryRunResult):
            return

        self.registry_name = other.registry_name
        self.published_paths = other.published_paths
        self.error = other.error

    def to_text(self) -> str | textstyler.StyledText:
        if self.error is not None:
            return f"Failed to publish to {self.registry_name}: {self.error}"

        if not self.published_paths:
            return f"Nothing to publish to {self.registry_name}"

        paths_str = ", ".join(str(path) for path in self.published_paths)
        return f"Published to {self.registry_name}: {paths_str}"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.error is not None:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PublishArtifactToRegistryAction(
    code_action.Action[
        PublishArtifactToRegistryRunPayload,
        PublishArtifactToRegistryRunContext,
        PublishArtifactToRegistryRunResult,
    ]
):
    """Publish a distribution artifact to a specific registry."""

    DESCRIPTION = "Publish a distribution artifact to a specific registry."
    PAYLOAD_TYPE = PublishArtifactToRegistryRunPayload
    RUN_CONTEXT_TYPE = PublishArtifactToRegistryRunContext
    RESULT_TYPE = PublishArtifactToRegistryRunResult
