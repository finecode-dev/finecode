from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import list_src_artifact_files_by_lang_action
from finecode_extension_api.interfaces import iprojectinfoprovider
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri


@dataclasses.dataclass
class ListSrcArtifactFilesByLangTomlHandlerConfig(code_action.ActionHandlerConfig):
    pass


class ListSrcArtifactFilesByLangTomlHandler(
    code_action.ActionHandler[
        list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangAction,
        ListSrcArtifactFilesByLangTomlHandlerConfig,
    ]
):
    def __init__(
        self,
        config: ListSrcArtifactFilesByLangTomlHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider

    async def run(
        self,
        payload: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload,
        run_context: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunContext,
    ) -> list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        toml_uris: list[ResourceUri] = [
            path_to_resource_uri(p)
            for p in project_dir.rglob("*.toml")
            if not any(part.startswith(".") for part in p.relative_to(project_dir).parts)
        ]
        return list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult(
            files_by_lang={"toml": toml_uris}
        )
