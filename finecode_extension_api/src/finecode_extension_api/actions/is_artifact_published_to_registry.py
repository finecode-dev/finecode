import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class IsArtifactPublishedToRegistryRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: pathlib.Path
    dist_artifact_paths: list[pathlib.Path]
    version: str
    registry_name: str


class IsArtifactPublishedToRegistryRunContext(
    code_action.RunActionContext[IsArtifactPublishedToRegistryRunPayload]
): ...


@dataclasses.dataclass
class IsArtifactPublishedToRegistryRunResult(code_action.RunActionResult):
    is_published_by_dist_path: dict[pathlib.Path, bool]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, IsArtifactPublishedToRegistryRunResult):
            return

        self.is_published_by_dist_path = other.is_published_by_dist_path

    def to_text(self) -> str | textstyler.StyledText:
        published = [
            p for p, is_pub in self.is_published_by_dist_path.items() if is_pub
        ]
        not_published = [
            p for p, is_pub in self.is_published_by_dist_path.items() if not is_pub
        ]
        parts: list[str] = []
        if published:
            parts.append(f"published: {', '.join(str(p) for p in published)}")
        if not_published:
            parts.append(f"not published: {', '.join(str(p) for p in not_published)}")
        return "; ".join(parts) if parts else "no artifacts"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class IsArtifactPublishedToRegistryAction(
    code_action.Action[
        IsArtifactPublishedToRegistryRunPayload,
        IsArtifactPublishedToRegistryRunContext,
        IsArtifactPublishedToRegistryRunResult,
    ]
):
    PAYLOAD_TYPE = IsArtifactPublishedToRegistryRunPayload
    RUN_CONTEXT_TYPE = IsArtifactPublishedToRegistryRunContext
    RESULT_TYPE = IsArtifactPublishedToRegistryRunResult
