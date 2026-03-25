# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class GetSrcArtifactVersionRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri


class GetSrcArtifactVersionRunContext(
    code_action.RunActionContext[GetSrcArtifactVersionRunPayload]
): ...


@dataclasses.dataclass
class GetSrcArtifactVersionRunResult(code_action.RunActionResult):
    version: str

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetSrcArtifactVersionRunResult):
            return

        self.version = other.version

    def to_text(self) -> str | textstyler.StyledText:
        return self.version

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetSrcArtifactVersionAction(
    code_action.Action[
        GetSrcArtifactVersionRunPayload,
        GetSrcArtifactVersionRunContext,
        GetSrcArtifactVersionRunResult,
    ]
):
    """Read the current version from an source artifact definition file."""

    PAYLOAD_TYPE = GetSrcArtifactVersionRunPayload
    RUN_CONTEXT_TYPE = GetSrcArtifactVersionRunContext
    RESULT_TYPE = GetSrcArtifactVersionRunResult
