import asyncio
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import group_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import lint_files_action
from finecode_extension_api.interfaces import iactionrunner, ilogger


@dataclasses.dataclass
class LintFilesDispatchHandlerConfig(code_action.ActionHandlerConfig): ...


class LintFilesDispatchHandler(
    code_action.ActionHandler[
        lint_files_action.LintFilesAction,
        LintFilesDispatchHandlerConfig,
    ]
):
    """Group files by language once and dispatch to lint_{lang}_files subactions concurrently.

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

    async def run(
        self,
        payload: lint_files_action.LintFilesRunPayload,
        run_context: lint_files_action.LintFilesRunContext,
    ) -> lint_files_action.LintFilesRunResult:
        # Discover registered lint_{lang}_files subactions by naming convention.
        all_names = self.action_runner.get_actions_names()
        lang_to_action_name: dict[str, str] = {
            name[len("lint_") : -len("_files")]: name
            for name in all_names
            if name.startswith("lint_") and name.endswith("_files") and name != "lint_files"
        }

        if not lang_to_action_name:
            self.logger.debug(f"LintFilesDispatchHandler: no lint_{lang}_files actions registered")
            return lint_files_action.LintFilesRunResult(messages={})

        # Group files by language — single pass, O(files).
        group_action = self.action_runner.get_action_by_source(
            group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction,
        )
        files_by_lang_result = await self.action_runner.run_action(
            action=group_action,
            payload=group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunPayload(
                file_paths=payload.file_paths,
                langs=list(lang_to_action_name.keys()),
            ),
            meta=run_context.meta,
        )
        files_by_lang = files_by_lang_result.files_by_lang

        # Dispatch concurrently — each subaction receives only its language's files.
        lint_tasks: list[asyncio.Task[lint_files_action.LintFilesRunResult]] = []
        try:
            async with asyncio.TaskGroup() as tg:
                for lang, files in files_by_lang.items():
                    if not files:
                        continue
                    subaction = self.action_runner.get_action_by_name(
                        lang_to_action_name[lang],
                        lint_files_action.LintFilesAction,
                    )
                    lint_tasks.append(
                        tg.create_task(
                            self.action_runner.run_action(
                                action=subaction,
                                payload=lint_files_action.LintFilesRunPayload(file_paths=files),
                                meta=run_context.meta,
                            )
                        )
                    )
        except ExceptionGroup as eg:
            error_str = ". ".join([str(e) for e in eg.exceptions])
            raise code_action.ActionFailedException(error_str) from eg

        result = lint_files_action.LintFilesRunResult(messages={})
        for task in lint_tasks:
            result.update(task.result())
        return result
