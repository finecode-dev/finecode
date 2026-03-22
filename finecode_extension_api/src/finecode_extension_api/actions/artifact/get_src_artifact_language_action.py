# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class GetSrcArtifactLanguageRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: ResourceUri


class GetSrcArtifactLanguageRunContext(
    code_action.RunActionContext[GetSrcArtifactLanguageRunPayload]
): ...


@dataclasses.dataclass
class GetSrcArtifactLanguageRunResult(code_action.RunActionResult):
    # Language identifier, e.g. "python", "javascript", "rust".
    language: str

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetSrcArtifactLanguageRunResult):
            return

        self.language = other.language

    def to_text(self) -> str | textstyler.StyledText:
        return self.language

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetSrcArtifactLanguageAction(
    code_action.Action[
        GetSrcArtifactLanguageRunPayload,
        GetSrcArtifactLanguageRunContext,
        GetSrcArtifactLanguageRunResult,
    ]
):
    """Detect the programming language of a source artifact."""

    PAYLOAD_TYPE = GetSrcArtifactLanguageRunPayload
    RUN_CONTEXT_TYPE = GetSrcArtifactLanguageRunContext
    RESULT_TYPE = GetSrcArtifactLanguageRunResult
