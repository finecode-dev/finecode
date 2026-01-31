import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class BuildAndPublishArtifactRunPayload(code_action.RunActionPayload):
    # if not provided, current artifact will be built and published
    src_artifact_def_path: pathlib.Path | None = None
    force: bool = False


class BuildAndPublishArtifactRunContext(
    code_action.RunActionContext[BuildAndPublishArtifactRunPayload]
): ...


@dataclasses.dataclass
class BuildAndPublishArtifactRunResult(code_action.RunActionResult):
    version: str
    published_registries: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, BuildAndPublishArtifactRunResult):
            return

        self.version = other.version
        self.published_registries = other.published_registries

    def to_text(self) -> str | textstyler.StyledText:
        if len(self.published_registries) > 0:
            registries_str = ", ".join(self.published_registries)
            return f"Built and published version {self.version} to: {registries_str}"
        else:
            return f"Built version {self.version}, already published to all registries"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class BuildAndPublishArtifactAction(
    code_action.Action[
        BuildAndPublishArtifactRunPayload,
        BuildAndPublishArtifactRunContext,
        BuildAndPublishArtifactRunResult,
    ]
):
    PAYLOAD_TYPE = BuildAndPublishArtifactRunPayload
    RUN_CONTEXT_TYPE = BuildAndPublishArtifactRunContext
    RESULT_TYPE = BuildAndPublishArtifactRunResult
