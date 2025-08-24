import dataclasses

import tomlkit

from finecode_extension_api import code_action
from finecode_extension_api.actions import dump_config as dump_config_action
from finecode_extension_api.interfaces import ifilemanager


@dataclasses.dataclass
class DumpConfigSaveHandlerConfig(code_action.ActionHandlerConfig): ...


class DumpConfigSaveHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, DumpConfigSaveHandlerConfig
    ]
):
    def __init__(
        self,
        file_manager: ifilemanager.IFileManager,
    ) -> None:
        self.file_manager = file_manager

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        raw_config_str = tomlkit.dumps(run_context.raw_config_dump)
        target_file_dir_path = payload.target_file_path.parent

        await self.file_manager.create_dir(dir_path=target_file_dir_path)
        await self.file_manager.save_file(
            file_path=payload.target_file_path, file_content=raw_config_str
        )

        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
