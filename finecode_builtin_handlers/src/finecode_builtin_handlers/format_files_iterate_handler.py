import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_files_action
from finecode_extension_api.actions.code_quality.format_file_action import (
    FormatFileAction,
    FormatFileCallerRunContextKwargs,
    FormatFileRunPayload,
    FormatFileRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class FormatFilesIterateHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatFilesIterateHandler(
    code_action.ActionHandler[
        format_files_action.FormatFilesAction,
        FormatFilesIterateHandlerConfig,
    ]
):
    """Iterate over all files and delegate each to FormatFileAction.

    FormatFileAction detects the file language and dispatches to the appropriate
    language-specific subaction, then runs generic handlers (e.g. save).
    Files are processed concurrently; handlers within each file run sequentially.

    The parent's file editor session is passed to each FormatFileAction via
    ``caller_kwargs`` so that all files share one session. Each file is
    read and blocked individually in FormatFileRunContext.init(), and the block
    is released when that file's run context exits.
    """

    def __init__(
        self,
        action_runner: iprojectactionrunner.IProjectActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _format_one_file(
        self,
        file_uri: ResourceUri,
        save: bool,
        meta: code_action.RunActionMeta,
        run_context: format_files_action.FormatFilesRunContext,
    ) -> format_files_action.FormatFilesRunResult:
        item_result: FormatFileRunResult = await self.action_runner.run_action(
            action_type=FormatFileAction,
            payload=FormatFileRunPayload(
                file_path=file_uri,
                save=save,
            ),
            meta=meta,
            caller_kwargs=FormatFileCallerRunContextKwargs(
                file_editor_session=run_context.file_editor_session,
            ),
        )
        return format_files_action.FormatFilesRunResult(
            result_by_file_path={
                file_uri: format_files_action.FormatRunFileResult(
                    changed=item_result.changed,
                    code=item_result.code,
                )
            }
        )

    async def run(
        self,
        payload: format_files_action.FormatFilesRunPayload,
        run_context: format_files_action.FormatFilesRunContext,
    ) -> None:
        for file_uri in payload.file_paths:
            run_context.partial_result_scheduler.schedule(
                file_uri,
                self._format_one_file(
                    file_uri, payload.save, run_context.meta, run_context
                ),
            )
