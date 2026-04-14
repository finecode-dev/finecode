import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import get_lint_fixes_action
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class GetLintFixesFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class GetLintFixesFilesDispatchHandler(
    code_action.ActionHandler[
        get_lint_fixes_action.GetLintFixesAction,
        GetLintFixesFilesDispatchHandlerConfig,
    ]
):
    """Dispatch ``get_lint_fixes`` to language-specific subactions.

    Groups the requested file by language via ``GroupSrcArtifactFilesByLangAction``
    and invokes the matching language subaction (e.g. ``GetLintFixesPythonFilesAction``).
    Partial results from the subaction are forwarded to the caller.
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
        payload: get_lint_fixes_action.GetLintFixesRunPayload,
        run_context: get_lint_fixes_action.GetLintFixesRunContext,
    ) -> None:
        subactions_by_lang = self.action_runner.get_actions_for_parent(
            get_lint_fixes_action.GetLintFixesAction
        )

        if not subactions_by_lang:
            self.logger.debug(
                "GetLintFixesFilesDispatchHandler: no language subactions registered"
            )
            return

        # Group the single file by language to find the correct language subaction.
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

        file_lang = next(
            (
                lang
                for lang, files in files_by_lang_result.files_by_lang.items()
                if files
            ),
            None,
        )
        if file_lang is None or file_lang not in subactions_by_lang:
            self.logger.debug(
                f"GetLintFixesFilesDispatchHandler: no subaction for file "
                f"{payload.file_path} (detected lang: {file_lang!r})"
            )
            return

        subaction = subactions_by_lang[file_lang]
        async for partial in self.action_runner.run_action_iter(
            action=subaction,
            payload=payload,
            meta=run_context.meta,
        ):
            await run_context.partial_result_sender.send(partial)
