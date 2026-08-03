# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from fine_format import format_file_action
from fine_format.format_file_action import FormatFileAction
from finecode_extension_api.interfaces import ifileeditor, ilogger
from finecode_extension_api.resource_uri import resource_uri_to_path


@dataclasses.dataclass
class SaveFormatFileHandlerConfig(code_action.ActionHandlerConfig): ...


class SaveFormatFileHandler(
    code_action.ActionHandler[FormatFileAction, SaveFormatFileHandlerConfig]
):
    def __init__(self, logger: ilogger.ILogger) -> None:
        self.logger = logger

    async def run(
        self,
        payload: format_file_action.FormatFileRunPayload,
        run_context: format_file_action.FormatFileRunContext,
    ) -> format_file_action.FormatFileRunResult:
        if payload.save:
            # Conditional on the version the formatting was based on: if the file
            # changed underneath the pipeline, the formatted content is derived
            # from stale input and writing it would discard the other change.
            # Refusing loudly is preferred to last-writer-wins.
            try:
                await run_context.file_editor_session.save_file(
                    file_path=resource_uri_to_path(payload.file_path),
                    file_content=run_context.file_info.file_content,
                    if_version=run_context.file_info.file_version,
                )
            except ifileeditor.FileVersionConflict as conflict:
                self.logger.warning(
                    f"Not saving formatted {payload.file_path}: {conflict.message}"
                )

        return format_file_action.FormatFileRunResult(
            changed=False,  # this handler doesn't change files, only saves them
            code=run_context.file_info.file_content,
        )
