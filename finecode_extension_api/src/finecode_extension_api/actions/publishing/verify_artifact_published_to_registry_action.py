# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action, textstyler
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class VerifyArtifactPublishedToRegistryRunPayload(code_action.RunActionPayload):
    dist_artifact_paths: list[ResourceUri]
    registry_name: str
    version: str


class VerifyArtifactPublishedToRegistryRunContext(
    code_action.RunActionContext[VerifyArtifactPublishedToRegistryRunPayload]
): ...


@dataclasses.dataclass
class VerifyArtifactPublishedToRegistryRunResult(code_action.RunActionResult):
    errors: list[str]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, VerifyArtifactPublishedToRegistryRunResult):
            return
        self.errors.extend(other.errors)

    def to_text(self) -> str | textstyler.StyledText:
        if self.errors:
            return (
                f"Verification failed with {len(self.errors)} error(s):\n"
                + "\n".join(f"  - {e}" for e in self.errors)
            )
        return "Verification successful"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        if self.errors:
            return code_action.RunReturnCode.ERROR
        return code_action.RunReturnCode.SUCCESS


class VerifyArtifactPublishedToRegistryAction(
    code_action.Action[
        VerifyArtifactPublishedToRegistryRunPayload,
        VerifyArtifactPublishedToRegistryRunContext,
        VerifyArtifactPublishedToRegistryRunResult,
    ]
):
    """Verify that artifact distributions are available in a registry."""

    PAYLOAD_TYPE = VerifyArtifactPublishedToRegistryRunPayload
    RUN_CONTEXT_TYPE = VerifyArtifactPublishedToRegistryRunContext
    RESULT_TYPE = VerifyArtifactPublishedToRegistryRunResult
