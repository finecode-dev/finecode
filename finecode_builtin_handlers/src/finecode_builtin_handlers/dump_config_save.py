import dataclasses

import tomlkit

from finecode_extension_api import code_action
from finecode_extension_api.actions import dump_config as dump_config_action
from finecode_extension_api.interfaces import ifilemanager, ifileeditor


@dataclasses.dataclass
class DumpConfigSaveHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigSaveHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigSaveHandlerConfig
    ]
):
    FILE_OPERATION_AUTHOR = ifileeditor.FileOperationAuthor(
        id="DumpConfigSaveHandler"
    )
    
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
        file_editor: ifileeditor.IFileEditor
    ) -> None:
        self.file_manager = file_manager
        self.file_editor = file_editor

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        raw_config_str = tomlkit.dumps(run_context.raw_config_dump)
        target_file_dir_path = payload.target_file_path.parent

        await self.file_manager.create_dir(dir_path=target_file_dir_path)
        async with self.file_editor.session(
            author=self.FILE_OPERATION_AUTHOR
        ) as session:
            await session.save_file(
                file_path=payload.target_file_path, file_content=raw_config_str
            )

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
