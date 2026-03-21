# docs: docs/reference/actions.md
import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class PublishArtifactRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: pathlib.Path
    dist_artifact_paths: list[pathlib.Path]
    force: bool = False


class PublishArtifactRunContext(
    code_action.RunActionContext[PublishArtifactRunPayload]
): ...


@dataclasses.dataclass
class PublishArtifactRunResult(code_action.RunActionResult):
    version: str
    published_registries: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PublishArtifactRunResult):
            return

        self.version = other.version
        self.published_registries = other.published_registries

    def to_text(self) -> str | textstyler.StyledText:
        if len(self.published_registries) > 0:
            registries_str = ", ".join(self.published_registries)
            return f"Published version {self.version} to: {registries_str}"
        else:
            return f"Version {self.version} is already published"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class PublishArtifactAction(
    code_action.Action[
        PublishArtifactRunPayload,
        PublishArtifactRunContext,
        PublishArtifactRunResult,
    ]
):
    """Publish a distribution artifact to all configured registries."""

    PAYLOAD_TYPE = PublishArtifactRunPayload
    RUN_CONTEXT_TYPE = PublishArtifactRunContext
    RESULT_TYPE = PublishArtifactRunResult
