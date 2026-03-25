import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import lint_files_action
from finecode_extension_api.interfaces import iactionrunner, ilogger
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class LintFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class LintFilesDispatchHandler(
    code_action.ActionHandler[
        lint_files_action.LintFilesAction,
        LintFilesDispatchHandlerConfig,
    ]
):
    """Group files by language once and dispatch to lint_{lang}_files subactions.

    Subaction names follow the convention: language "python" maps to "lint_python_files",
    "javascript" maps to "lint_javascript_files", etc. Each subaction must be registered
    in the project config.
    """

    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        logger: ilogger.ILogger,
    ) -> None:
        self.action_runner = action_runner
        self.logger = logger

    async def _lint_file(
        self,
        subaction: iactionrunner.ActionDeclaration[lint_files_action.LintFilesAction],
        file_uri: ResourceUri,
        meta: code_action.RunActionMeta,
    ) -> lint_files_action.LintFilesRunResult:
        return await self.action_runner.run_action(
            action=subaction,
            payload=lint_files_action.LintFilesRunPayload(file_paths=[file_uri]),
            meta=meta,
        )

    async def run(
        self,
        payload: lint_files_action.LintFilesRunPayload,
        run_context: lint_files_action.LintFilesRunContext,
    ) -> None:
        subactions_by_lang = self.action_runner.get_actions_for_parent(
            lint_files_action.LintFilesAction
        )

        if not subactions_by_lang:
            self.logger.debug("LintFilesDispatchHandler: no language subactions registered")
            return

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

        # Build reverse mapping: file → language subaction.
        file_to_subaction: dict[ResourceUri, iactionrunner.ActionDeclaration[lint_files_action.LintFilesAction]] = {}
        for lang, files in files_by_lang.items():
            for file_uri in files:
                file_to_subaction[file_uri] = subactions_by_lang[lang]

        # Schedule per-file coroutines via partial_result_scheduler so that
        # run_action can execute them concurrently and send partial results.
        for file_uri in payload.file_paths:
            if file_uri in file_to_subaction:
                run_context.partial_result_scheduler.schedule(
                    file_uri,
                    self._lint_file(file_to_subaction[file_uri], file_uri, run_context.meta),
                )
