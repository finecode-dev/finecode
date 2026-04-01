# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_file_action
from finecode_extension_api.actions.code_quality.format_file_action import FormatFileAction
from finecode_extension_api.interfaces import ilogger
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
            await run_context.file_editor_session.save_file(
                file_path=resource_uri_to_path(payload.file_path),
                file_content=run_context.file_info.file_content,
            )

        return format_file_action.FormatFileRunResult(
            changed=False,  # this handler doesn't change files, only saves them
            code=run_context.file_info.file_content,
        )
