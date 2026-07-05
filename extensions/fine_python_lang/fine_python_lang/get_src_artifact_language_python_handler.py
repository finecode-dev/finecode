import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import get_src_artifact_language_action
from finecode_extension_api.resource_uri import resource_uri_to_path

PYTHON_DEF_FILENAMES = {"pyproject.toml", "setup.py", "setup.cfg"}


@dataclasses.dataclass
class GetSrcArtifactLanguagePythonHandlerConfig(code_action.ActionHandlerConfig): ...


class GetSrcArtifactLanguagePythonHandler(
    code_action.ActionHandler[
        get_src_artifact_language_action.GetSrcArtifactLanguageAction,
        GetSrcArtifactLanguagePythonHandlerConfig,
    ]
):
    """Detect Python artifacts by their definition file name."""

    async def run(
        self,
        payload: get_src_artifact_language_action.GetSrcArtifactLanguageRunPayload,
        run_context: get_src_artifact_language_action.GetSrcArtifactLanguageRunContext,
    ) -> get_src_artifact_language_action.GetSrcArtifactLanguageRunResult:
        def_path = resource_uri_to_path(payload.src_artifact_def_path)
        language = "python" if def_path.name in PYTHON_DEF_FILENAMES else ""
        return get_src_artifact_language_action.GetSrcArtifactLanguageRunResult(
            language=language
        )
