# note: ruff formatter cannot sort imports, only ruff linter with fixes:
# https://docs.astral.sh/ruff/formatter/#sorting-imports
from __future__ import annotations

import dataclasses
import sys

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

from fine_format import format_file_action
from fine_python_lang import support_range
from fine_python_lang.format_python_file_action import (
    FormatPythonFileAction,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_python_ruff import target_version as target_version_utils
from fine_python_ruff.ruff_lsp_service import RuffLspService


@dataclasses.dataclass
class RuffFormatFileHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    indent_width: int | None = None
    """Spaces per indentation level. None leaves it to the project's ruff config.

    None rather than ruff's own default of 4: these are sent as ruff configuration, and
    configuration from the editor outranks the project's own, so a value here would
    override ``[tool.ruff] indent-width`` in every project that sets one -- silently
    replacing a deliberate choice with a default nobody asked for."""
    quote_style: str | None = None
    """``"double"`` or ``"single"``. None leaves it to the project's ruff config; see
    ``indent_width`` for why that is the default rather than ruff's own."""
    target_version: str | None = None
    """Language level to format for, e.g. ``"py311"``.

    None derives it from the project's declared support range
    (``get_src_artifact_toolchain_range``)."""
    preview: bool = False


class RuffFormatFileHandler(
    code_action.ActionHandler[FormatPythonFileAction, RuffFormatFileHandlerConfig]
):
    def __init__(
        self,
        config: RuffFormatFileHandlerConfig,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        lsp_service: RuffLspService,
    ) -> None:
        self.config = config
        self.logger = logger
        self.project_info_provider = project_info_provider
        self.lsp_service = lsp_service

        self._support_range_resolver = support_range.PythonSupportRangeResolver(
            action_runner=action_runner, logger=logger
        )
        # registered here, resolved later: deriving the target version runs another
        # action and so cannot happen in a constructor. The shared service runs every
        # registered provider before it starts ruff, which is what makes these settings
        # apply even when another handler is the one that reaches the server first.
        self.lsp_service.add_settings_provider(self._provide_lsp_settings)

    async def _provide_lsp_settings(
        self, meta: code_action.RunActionMeta
    ) -> dict[str, object]:
        target_version = await target_version_utils.resolve_target_version(
            configured=self.config.target_version,
            resolver=self._support_range_resolver,
            meta=meta,
            logger=self.logger,
        )

        # reference: https://docs.astral.sh/ruff/editors/settings/
        settings: dict[str, object] = {"lineLength": self.config.line_length}
        if self.config.preview:
            settings["format"] = {"preview": True}

        # Ruff's *client* settings are a short list, and `targetVersion`, `indentWidth`
        # and `quoteStyle` are not on it -- unknown fields are dropped in silence, so
        # sending them there configures nothing at all. `configuration` is a ruff config
        # table by another name and carries all three, under their config-file spellings.
        configuration: dict[str, object] = {}
        if target_version is not None:
            configuration["target-version"] = target_version
        if self.config.indent_width is not None:
            configuration["indent-width"] = self.config.indent_width
        if self.config.quote_style is not None:
            configuration["format"] = {"quote-style": self.config.quote_style}
        if configuration:
            settings["configuration"] = configuration

        return settings

    @override
    async def run(
        self,
        payload: format_file_action.FormatFileRunPayload,
        run_context: format_file_action.FormatFileRunContext,
    ) -> format_file_action.FormatFileRunResult:
        root_uri = self.project_info_provider.get_current_project_dir_path().as_uri()
        await self.lsp_service.ensure_started(root_uri, run_context.meta)

        file_path = resource_uri_to_path(payload.file_path)
        file_content = run_context.file_info.file_content
        file_version = run_context.file_info.file_version

        new_file_content = await self.lsp_service.format_file(file_path, file_content)
        file_changed = new_file_content != file_content

        # update for next handlers in the pipeline
        run_context.file_info = format_file_action.FileInfo(
            new_file_content, file_version
        )

        return format_file_action.FormatFileRunResult(
            changed=file_changed, code=new_file_content if file_changed else ""
        )
