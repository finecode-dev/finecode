# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_files_action
from finecode_extension_api.interfaces import ifileeditor, ilogger


@dataclasses.dataclass
class SaveFormatFilesHandlerConfig(code_action.ActionHandlerConfig): ...


class SaveFormatFilesHandler(
    code_action.ActionHandler[
        format_files_action.FormatFilesAction, SaveFormatFilesHandlerConfig
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="SaveFormatFilesHandler")

    def __init__(
        self, file_editor: ifileeditor.IFileEditor, logger: ilogger.ILogger
    ) -> None:
        self.file_editor = file_editor
        self.logger = logger

    async def run(
        self,
        payload: format_files_action.FormatFilesRunPayload,
        run_context: format_files_action.FormatFilesRunContext,
    ) -> format_files_action.FormatFilesRunResult:
        file_paths = payload.file_paths
        save = payload.save

        if save is True:
            async with self.file_editor.session(self.FILE_OPERATION_AUTHOR) as session:
                for file_path in file_paths:
                    file_content = run_context.file_info_by_path[file_path].file_content
                    # TODO: only if changed?
                    await session.save_file(
                        file_path=file_path, file_content=file_content
                    )

        result = format_files_action.FormatFilesRunResult(
            result_by_file_path={
                file_path: format_files_action.FormatRunFileResult(
                    changed=False,  # this handler doesn't change files, only saves them
                    code=run_context.file_info_by_path[file_path].file_content,
                )
                for file_path in file_paths
            }
        )
        return result
