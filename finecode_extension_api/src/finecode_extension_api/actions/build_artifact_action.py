import dataclasses
import pathlib

from finecode_extension_api import code_action, textstyler


@dataclasses.dataclass
class BuildArtifactRunPayload(code_action.RunActionPayload):
    # if not provided, current artifact will be built
    src_artifact_def_path: pathlib.Path | None = None


class BuildArtifactRunContext(
    code_action.RunActionContext[BuildArtifactRunPayload]
): ...


@dataclasses.dataclass
class BuildArtifactRunResult(code_action.RunActionResult):
    build_output_paths: list[pathlib.Path]

    def update(self, other: code_action.RunActionResult) -> None:
        if not isinstance(other, BuildArtifactRunResult):
            return

        self.build_output_paths = other.build_output_paths

    def to_text(self) -> str | textstyler.StyledText:
        paths_str = "\n  ".join(str(p) for p in self.build_output_paths)
        return f"Built artifact at:\n  {paths_str}"

    @property
    def return_code(self) -> code_action.RunReturnCode:
        return code_action.RunReturnCode.SUCCESS


class BuildArtifactAction(
    code_action.Action[
        BuildArtifactRunPayload,
        BuildArtifactRunContext,
        BuildArtifactRunResult,
    ]
):
    PAYLOAD_TYPE = BuildArtifactRunPayload
    RUN_CONTEXT_TYPE = BuildArtifactRunContext
    RESULT_TYPE = BuildArtifactRunResult
