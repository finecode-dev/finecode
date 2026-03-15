# docs: docs/reference/actions.md
import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class PublishArtifactToRegistryRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: pathlib.Path
    dist_artifact_paths: list[pathlib.Path]
    registry_name: str
    force: bool = False


class PublishArtifactToRegistryRunContext(
    code_action.RunActionContext[PublishArtifactToRegistryRunPayload]
): ...


@dataclasses.dataclass
class PublishArtifactToRegistryRunResult(code_action.RunActionResult):
    ...

    def update(self, other: code_action.RunActionResult) -> None: ...

    def to_text(self) -> str | textstyler.StyledText:
        return "Published"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class PublishArtifactToRegistryAction(
    code_action.Action[
        PublishArtifactToRegistryRunPayload,
        PublishArtifactToRegistryRunContext,
        PublishArtifactToRegistryRunResult,
    ]
):
    PAYLOAD_TYPE = PublishArtifactToRegistryRunPayload
    RUN_CONTEXT_TYPE = PublishArtifactToRegistryRunContext
    RESULT_TYPE = PublishArtifactToRegistryRunResult
