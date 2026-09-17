# docs: docs/reference/actions.md
import dataclasses

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, ifilemanager
from finecode_extension_api.resource_uri import resource_uri_to_path

from fine_envs import dump_config_action


@dataclasses.dataclass
class DumpConfigSaveHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigSaveHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigSaveHandlerConfig
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(id="DumpConfigSaveHandler")

    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
        file_editor: ifileeditor.IFileEditor,
    ) -> None:
        self.file_manager = file_manager
        self.file_editor = file_editor

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        if run_context.config_dump_content is None:
            raise code_action.ActionFailedException(
                "dump_config_save: no rendered dump content; the dump_config"
                " handler must run before this one"
            )
        target_file_path = resource_uri_to_path(payload.target_file_path)
        target_file_dir_path = target_file_path.parent

        await self.file_manager.create_dir(dir_path=target_file_dir_path)
        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            await session.save_file(
                file_path=target_file_path,
                file_content=run_context.config_dump_content,
            )

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
