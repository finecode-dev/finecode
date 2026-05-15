from __future__ import annotations

import dataclasses
from typing import override

from finecode_extension_api import code_action
from fine_format import format_file_action
from fine_toml_lang.format_toml_file_action import FormatTomlFileAction
from finecode_extension_api.interfaces import iprojectinfoprovider
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_toml_tombi.tombi_lsp_service import TombiLspService


@dataclasses.dataclass
class TombiFormatTomlFileHandlerConfig(code_action.ActionHandlerConfig):
    pass


class TombiFormatTomlFileHandler(
    code_action.ActionHandler[FormatTomlFileAction, TombiFormatTomlFileHandlerConfig]
):
    def __init__(
        self,
        config: TombiFormatTomlFileHandlerConfig,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: TombiLspService,
    ) -> None:
        self.config = config
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

    @override
    async def run(
        self,
        payload: format_file_action.FormatFileRunPayload,
        run_context: format_file_action.FormatFileRunContext,
    ) -> format_file_action.FormatFileRunResult:
        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri)

        file_path = resource_uri_to_path(payload.file_path)
        file_content = run_context.file_info.file_content
        file_version = run_context.file_info.file_version

        new_file_content = await self.lsp_service.format_file(file_path, file_content)
        file_changed = new_file_content != file_content

        run_context.file_info = format_file_action.FileInfo(new_file_content, file_version)

        return format_file_action.FormatFileRunResult(
            changed=file_changed, code=new_file_content
        )
