import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class GetDistArtifactVersionRunPayload(code_action.RunActionPayload):
    dist_artifact_path: pathlib.Path


class GetDistArtifactVersionRunContext(
    code_action.RunActionContext[GetDistArtifactVersionRunPayload]
): ...


@dataclasses.dataclass
class GetDistArtifactVersionRunResult(code_action.RunActionResult):
    version: str

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetDistArtifactVersionRunResult):
            return

        self.version = other.version

    def to_text(self) -> str | textstyler.StyledText:
        return self.version

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetDistArtifactVersionAction(
    code_action.Action[
        GetDistArtifactVersionRunPayload,
        GetDistArtifactVersionRunContext,
        GetDistArtifactVersionRunResult,
    ]
):
    PAYLOAD_TYPE = GetDistArtifactVersionRunPayload
    RUN_CONTEXT_TYPE = GetDistArtifactVersionRunContext
    RESULT_TYPE = GetDistArtifactVersionRunResult
