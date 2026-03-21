# docs: docs/reference/actions.md
import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class Registry:
    url: str
    name: str


@dataclasses.dataclass
class GetSrcArtifactRegistriesRunPayload(code_action.RunActionPayload):
    src_artifact_def_path: pathlib.Path


class GetSrcArtifactRegistriesRunContext(
    code_action.RunActionContext[GetSrcArtifactRegistriesRunPayload]
): ...


@dataclasses.dataclass
class GetSrcArtifactRegistriesRunResult(code_action.RunActionResult):
    registries: list[Registry]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, GetSrcArtifactRegistriesRunResult):
            return

        self.registries = other.registries

    def to_text(self) -> str | textstyler.StyledText:
        if not self.registries:
            return "No registries configured"

        lines: list[str] = []
        for registry in self.registries:
            lines.append(f"{registry.name}: {registry.url}")
        return "\n".join(lines)

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class GetSrcArtifactRegistriesAction(
    code_action.Action[
        GetSrcArtifactRegistriesRunPayload,
        GetSrcArtifactRegistriesRunContext,
        GetSrcArtifactRegistriesRunResult,
    ]
):
    """List the registries configured for an artifact."""

    PAYLOAD_TYPE = GetSrcArtifactRegistriesRunPayload
    RUN_CONTEXT_TYPE = GetSrcArtifactRegistriesRunContext
    RESULT_TYPE = GetSrcArtifactRegistriesRunResult
