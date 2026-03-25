# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import list_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import lint_action, lint_files_action
from finecode_extension_api.interfaces import iactionrunner, ifileeditor, ilogger
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri


@dataclasses.dataclass
class LintHandlerConfig(code_action.ActionHandlerConfig): ...


class LintHandler(
    code_action.ActionHandler[
        lint_action.LintAction, LintHandlerConfig
    ]
):
    def __init__(
        self,
        action_runner: iactionrunner.IActionRunner,
        logger: ilogger.ILogger,
        file_editor: ifileeditor.IFileEditor,
    ) -> None:
        self.action_runner = action_runner
        self.file_editor = file_editor
        self.logger = logger

    async def run(
        self,
        payload: lint_action.LintRunPayload,
        run_context: lint_action.LintRunContext,
    ):
        run_meta = run_context.meta
        file_uris: list[ResourceUri]

        if payload.target == lint_action.LintTarget.PROJECT:
            if (
                run_meta.dev_env == code_action.DevEnv.IDE
                and run_meta.trigger == code_action.RunActionTrigger.SYSTEM
            ):
                # Performance optimisation: when the IDE triggers a background project
                # lint automatically, only lint the currently opened files.
                file_uris = [
                    path_to_resource_uri(p)
                    for p in self.file_editor.get_opened_files()
                ]
            else:
                list_action = self.action_runner.get_action_by_source(
                    list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangAction,
                )
                files_by_lang_result = await self.action_runner.run_action(
                    action=list_action,
                    payload=list_src_artifact_files_by_lang_action.ListSrcArtifactFilesByLangRunPayload(
                        langs=None
                    ),
                    meta=run_meta,
                )
                file_uris = [
                    f
                    for files in files_by_lang_result.files_by_lang.values()
                    for f in files
                ]
        else:
            file_uris = payload.file_paths

        lint_files_action_instance = self.action_runner.get_action_by_source(
            lint_files_action.LintFilesAction
        )
        async for partial in self.action_runner.run_action_iter(
            action=lint_files_action_instance,
            payload=lint_files_action.LintFilesRunPayload(file_paths=file_uris),
            meta=run_meta,
        ):
            yield lint_action.LintRunResult(messages=partial.messages)
