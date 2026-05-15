from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path


@dataclasses.dataclass
class GroupSrcArtifactFilesByLangTomlHandlerConfig(code_action.ActionHandlerConfig):
    pass


class GroupSrcArtifactFilesByLangTomlHandler(
    code_action.ActionHandler[
        group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction,
        GroupSrcArtifactFilesByLangTomlHandlerConfig,
    ]
):
    async def run(
        self,
        payload: group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload,
        run_context: group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunContext,
    ) -> group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult:
        toml_uris: list[ResourceUri] = [
            uri
            for uri in payload.file_paths
            if resource_uri_to_path(uri).suffix == ".toml"
        ]
        return group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult(
            files_by_lang={"toml": toml_uris}
        )
