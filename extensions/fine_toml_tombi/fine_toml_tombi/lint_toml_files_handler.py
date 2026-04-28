from __future__ import annotations

import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import lint_files_action
from fine_toml_lang.lint_toml_files_action import LintTomlFilesAction
from finecode_extension_api.interfaces import iprojectinfoprovider
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_toml_tombi.tombi_lsp_service import TombiLspService


@dataclasses.dataclass
class TombiLintTomlFilesHandlerConfig(code_action.ActionHandlerConfig):
    pass


class TombiLintTomlFilesHandler(
    code_action.ActionHandler[LintTomlFilesAction, TombiLintTomlFilesHandlerConfig]
):
    def __init__(
        self,
        config: TombiLintTomlFilesHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: TombiLspService,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

    async def run_on_single_file(
        self, file_uri: ResourceUri
    ) -> lint_files_action.LintFilesRunResult:
        file_path = resource_uri_to_path(file_uri)
        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)
        lint_messages = await self.lsp_service.check_file(file_path)
        return lint_files_action.LintFilesRunResult(
            messages={file_uri: lint_messages}
        )

    async def run(
        self,
        payload: lint_files_action.LintFilesRunPayload,
        run_context: lint_files_action.LintFilesRunContext,
    ) -> None:
        file_uris = [file_uri async for file_uri in payload]
        for file_uri in file_uris:
            run_context.partial_result_scheduler.schedule(
                file_uri,
                self.run_on_single_file(file_uri),
            )
