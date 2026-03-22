import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import format_files_action
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class FormatFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatFilesDispatchHandler(
    code_action.ActionHandler[
        format_files_action.FormatFilesAction,
        FormatFilesDispatchHandlerConfig,
    ]
):
    """Group files by language once and dispatch to format_{lang}_files subactions sequentially.

    Subaction names follow the convention: language "python" maps to "format_python_files",
    "javascript" maps to "format_javascript_files", etc. Each subaction must be registered
    in the project config.
    """

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def run(
        self,
        payload: format_files_action.FormatFilesRunPayload,
        run_context: format_files_action.FormatFilesRunContext,
    ) -> format_files_action.FormatFilesRunResult:
        subactions_by_lang = self.action_runner.get_actions_for_parent(
            format_files_action.FormatFilesAction
        )

        if not subactions_by_lang:
            self.logger.debug("FormatFilesDispatchHandler: no language subactions registered")
            return format_files_action.FormatFilesRunResult(result_by_file_path={})

        # Group files by language — single pass, O(files).
        group_action = self.action_runner.get_action_by_source(
            group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction,
        )
        files_by_lang_result = await self.action_runner.run_action(
            action=group_action,
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=payload.file_paths,
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )
        files_by_lang = files_by_lang_result.files_by_lang

        # Dispatch sequentially (format actions modify files on disk).
        format_tasks: list[asyncio.Task[format_files_action.FormatFilesRunResult]] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for lang, files in files_by_lang.items():
                    if not files:
                        continue
                    format_tasks.append(
                        tg.create_task(
                            self.action_runner.run_action(
                                action=subactions_by_lang[lang],
                                payload=format_files_action.FormatFilesRunPayload(
                                    file_paths=files,
                                    save=payload.save,
                                ),
                                meta=run_context.meta,
                            )
                        )
                    )
        except ExceptionGroup as eg:
            error_str = ". ".join([str(e) for e in eg.exceptions])
            raise code_action.ActionFailedException(error_str) from eg

        result = format_files_action.FormatFilesRunResult(result_by_file_path={})
        for task in format_tasks:
            result.update(task.result())
        return result
