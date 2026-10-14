from __future__ import annotations

import dataclasses

from fine_src_artifacts import list_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectinfoprovider,
    iworkspaceinfoprovider,
)
from finecode_extension_api.interfaces.iworkspaceinfoprovider import (
    actionable_project_paths,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_api.workspace_utils import (
    nested_project_dirs,
    walk_project_files,
)


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
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.workspace_info_provider = workspace_info_provider

    async def run(
        self,
        payload: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload,
        run_context: list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunContext,
    ) -> list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()
        toml_files = walk_project_files(
            project_dir,
            suffix=".toml",
            excluded_dirs=nested_project_dirs(
                project_dir,
                actionable_project_paths(
                    await self.workspace_info_provider.get_workspace_projects()
                ),
            ),
        )
        toml_uris: list[ResourceUri] = [path_to_resource_uri(p) for p in toml_files]
        return (
            list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunResult(
                files_by_lang={"toml": toml_uris}
            )
        )
