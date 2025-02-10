from __future__ import annotations
from io import StringIO

import isort.api as isort_api
import isort.settings as isort_settings

from finecode.extension_runner.interfaces import icache, ilogger, ifilemanager
from finecode import (
    CodeActionConfig,
    CodeFormatAction,
    FormatRunPayload,
    FormatRunResult,
    CodeActionConfigType,
    ActionContext,
)


class IsortCodeActionConfig(CodeActionConfig): ...


class IsortCodeAction(CodeFormatAction[IsortCodeActionConfig]):
    CACHE_KEY = "Isort"

    def __init__(
        self,
        config: CodeActionConfigType,
        context: ActionContext,
        logger: ilogger.ILogger,
        file_manager: ifilemanager.IFileManager,
        cache: icache.ICache,
    ) -> None:
        super().__init__(config, context)
        self.logger = logger
        self.file_manager = file_manager
        self.cache = cache

    async def run(self, payload: FormatRunPayload) -> FormatRunResult:
        file_path = payload.apply_on
        try:
            new_file_content = await self.cache.get_file_cache(file_path, self.CACHE_KEY)
            return FormatRunResult(changed=False, code=new_file_content)
        except icache.CacheMissException:
            pass

        file_content = await self.file_manager.get_content(file_path)
        file_version = await self.file_manager.get_file_version(file_path)
        file_changed = False
        new_file_content = file_content

        input_stream = StringIO(file_content)
        output_stream_context = isort_api._in_memory_output_stream_context()
        with output_stream_context as output_stream:
            changed = isort_api.sort_stream(
                input_stream=input_stream,
                output_stream=output_stream,
                config=isort_settings.Config(),  # TODO: config
                file_path=file_path,
                disregard_skip=True,
                extension=".py",
            )
            output_stream.seek(0)
            if changed:
                ...

        await self.cache.save_file_cache(file_path, file_version, self.CACHE_KEY, new_file_content)
        return FormatRunResult(changed=file_changed, code=new_file_content)
