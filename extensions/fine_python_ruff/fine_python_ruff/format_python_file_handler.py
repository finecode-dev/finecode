# note: ruff formatter cannot sort imports, only ruff linter with fixes:
# https://docs.astral.sh/ruff/formatter/#sorting-imports
from __future__ import annotations

import dataclasses
import sys

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_file_action
from fine_python_lang.format_python_file_action import (
    FormatPythonFileAction,
)
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path
from fine_python_ruff.ruff_lsp_service import RuffLspService


@dataclasses.dataclass
class RuffFormatFileHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    indent_width: int = 4
    quote_style: str = "double"  # "double" or "single"
    target_version: str = "py38"  # minimum Python version
    preview: bool = False


class RuffFormatFileHandler(
    code_action.ActionHandler[FormatPythonFileAction, RuffFormatFileHandlerConfig]
):
    def __init__(
        self,
        config: RuffFormatFileHandlerConfig,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: RuffLspService,
    ) -> None:
        self.config = config
        self.logger = logger
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

        # reference: https://docs.astral.sh/ruff/editors/settings/
        format_settings: dict[str, object] = {
            "indentWidth": self.config.indent_width,
            "quoteStyle": self.config.quote_style,
        }
        if self.config.preview:
            format_settings["preview"] = True
        settings: dict[str, object] = {
            "lineLength": self.config.line_length,
            "targetVersion": self.config.target_version,
            "format": format_settings,
        }
        self.lsp_service.update_settings(settings)

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

        # update for next handlers in the pipeline
        run_context.file_info = format_file_action.FileInfo(new_file_content, file_version)

        return format_file_action.FormatFileRunResult(
            changed=file_changed, code=new_file_content
        )
