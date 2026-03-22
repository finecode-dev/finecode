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
    verification_errors: dict[str, list[str]]  # registry_name -> errors

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, PublishAndVerifyArtifactRunResult):
            return
        self.version = other.version
        self.published_registries = other.published_registries
        self.verification_errors = other.verification_errors

    def to_text(self) -> str | textstyler.StyledText:
        lines = []
        if self.published_registries:
            lines.append(f"Published version {self.version} to: {', '.join(self.published_registries)}")
        else:
            lines.append(f"Version {self.version} was already published")

        if self.verification_errors:
            lines.append("Verification errors:")
            for registry, errors in self.verification_errors.items():
                for error in errors:
                    lines.append(f"  - {registry}: {error}")
        else:
            lines.append("Verification successful for all registries")

        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.verification_errors:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class PublishAndVerifyArtifactAction(
    code_action.Action[
        PublishAndVerifyArtifactRunPayload,
        PublishAndVerifyArtifactRunContext,
        PublishAndVerifyArtifactRunResult,
    ]
):
    PAYLOAD_TYPE = PublishAndVerifyArtifactRunPayload
    RUN_CONTEXT_TYPE = PublishAndVerifyArtifactRunContext
    RESULT_TYPE = PublishAndVerifyArtifactRunResult
