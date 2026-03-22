# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.artifact import list_src_artifact_files_by_lang_action
from finecode_extension_api.actions.code_quality import format_action, format_files_action
from finecode_extension_api.interfaces import iactionrunner, ifileeditor, ilogger
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri


@dataclasses.dataclass
class FormatHandlerConfig(code_action.ActionHandlerConfig): ...


class FormatHandler(
    code_action.ActionHandler[format_action.FormatAction, FormatHandlerConfig]
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
        payload: format_action.FormatRunPayload,
        run_context: format_action.FormatRunContext,
    ) -> format_action.FormatRunResult:
        run_meta = run_context.meta
        file_uris: list[ResourceUri]

        if payload.target == format_action.FormatTarget.PROJECT:
            if (
                run_meta.dev_env == code_action.DevEnv.IDE
                and run_meta.trigger == code_action.RunActionTrigger.SYSTEM
            ):
                # Performance optimisation: when the IDE triggers a background project
                # format automatically, only format the currently opened files.
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

        format_files_action_instance = self.action_runner.get_action_by_source(
            format_files_action.FormatFilesAction
        )
        format_result = await self.action_runner.run_action(
            action=format_files_action_instance,
            payload=format_files_action.FormatFilesRunPayload(
                file_paths=file_uris,
                save=payload.save,
            ),
            meta=run_meta,
        )
        return format_action.FormatRunResult(
            result_by_file_path=format_result.result_by_file_path
        )
