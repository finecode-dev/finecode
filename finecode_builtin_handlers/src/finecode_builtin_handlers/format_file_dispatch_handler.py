import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import format_file_action
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class FormatFileDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatFileDispatchHandler(
    code_action.ActionHandler[
        format_file_action.FormatFileAction,
        FormatFileDispatchHandlerConfig,
    ]
):
    """Detect language of the file and dispatch to the language-specific format_file subaction.

    After the subaction returns, updates run_context.file_info so that downstream
    handlers (e.g. save) see the formatted content.
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
        payload: format_file_action.FormatFileRunPayload,
        run_context: format_file_action.FormatFileRunContext,
    ) -> format_file_action.FormatFileRunResult:
        subactions_by_lang = self.action_runner.get_actions_for_parent(
            format_file_action.FormatFileAction
        )

        if not subactions_by_lang:
            self.logger.debug("FormatFileDispatchHandler: no language subactions registered")
            return format_file_action.FormatFileRunResult(
                changed=False, code=run_context.file_info.file_content
            )

        group_action = self.action_runner.get_action_by_source(
            group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction,
        )
        files_by_lang_result = await self.action_runner.run_action(
            action=group_action,
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=[payload.file_path],
                langs=list(subactions_by_lang.keys()),
            ),
            meta=run_context.meta,
        )

        lang_subaction = None
        for lang, files in files_by_lang_result.files_by_lang.items():
            if files:
                lang_subaction = subactions_by_lang[lang]
                break

        if lang_subaction is None:
            self.logger.debug(
                f"FormatFileDispatchHandler: no language subaction for {payload.file_path}"
            )
            return format_file_action.FormatFileRunResult(
                changed=False, code=run_context.file_info.file_content
            )

        result: format_file_action.FormatFileRunResult = await self.action_runner.run_action(
            action=lang_subaction,
            payload=format_file_action.FormatFileRunPayload(
                file_path=payload.file_path,
                save=payload.save,
            ),
            meta=run_context.meta,
            caller_kwargs=format_file_action.FormatFileCallerRunContextKwargs(
                file_editor_session=run_context.file_editor_session,
                file_info=run_context.file_info,
            ),
        )

        # bridge: update context so the downstream handlers see the formatted content
        if result.changed:
            run_context.file_info = format_file_action.FileInfo(
                file_content=result.code,
                file_version=run_context.file_info.file_version,
            )

        return result
