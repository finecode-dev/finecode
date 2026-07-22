# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class ListPublishedArtifactsRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri
    version: str
    registry_name: str


class ListPublishedArtifactsRunContext(
    code_action.RunActionContext[ListPublishedArtifactsRunPayload]
): ...


@dataclasses.dataclass
class ListPublishedArtifactsRunResult(code_action.RunActionResult):
    filenames: list[str]
    """Distribution filenames the registry holds for this version. Empty means
    the version is not published at all; a caller building the same package
    can derive which of its own dist paths still need uploading by checking
    filename membership here."""

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, ListPublishedArtifactsRunResult):
            return

        self.filenames = other.filenames

    def to_text(self) -> str | textstyler.StyledText:
        if not self.filenames:
            return "not published"
        return "published: " + ", ".join(self.filenames)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class ListPublishedArtifactsAction(
    code_action.Action[
        ListPublishedArtifactsRunPayload,
        ListPublishedArtifactsRunContext,
        ListPublishedArtifactsRunResult,
    ]
):
    """List the distribution filenames a registry holds for a given version."""

    DESCRIPTION = "List the distribution filenames a registry holds for a given version."
    PAYLOAD_TYPE = ListPublishedArtifactsRunPayload
    RUN_CONTEXT_TYPE = ListPublishedArtifactsRunContext
    RESULT_TYPE = ListPublishedArtifactsRunResult
