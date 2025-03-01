from __future__ import annotations

from io import StringIO

import isort.api as isort_api
import isort.settings as isort_settings

from finecode import (
    ActionContext,
    CodeActionConfig,
    FormatCodeAction,
    FormatRunContext,
    FormatRunPayload,
    FormatRunResult,
)
from finecode.extension_runner.interfaces import icache, ilogger


class IsortCodeActionConfig(CodeActionConfig):
    profile: str = ""


class IsortCodeAction(FormatCodeAction[IsortCodeActionConfig]):
    CACHE_KEY = "Isort"

    def __init__(
        self,
        config: IsortCodeActionConfig,
        context: ActionContext,
        logger: ilogger.ILogger,
        cache: icache.ICache,
    ) -> None:
        super().__init__(config, context)
        self.logger = logger
        self.cache = cache

    async def run(
        self, payload: FormatRunPayload, run_context: FormatRunContext
    ) -> FormatRunResult:
        file_path = payload.file_path
        try:
            new_file_content = await self.cache.get_file_cache(
                file_path, self.CACHE_KEY
            )
            return FormatRunResult(changed=False, code=new_file_content)
        except icache.CacheMissException:
            pass

        file_content = run_context.file_content
        file_version = run_context.file_version
        file_changed = False
        new_file_content = file_content

        input_stream = StringIO(file_content)
        output_stream_context = isort_api._in_memory_output_stream_context()
        with output_stream_context as output_stream:
            changed = isort_api.sort_stream(
                input_stream=input_stream,
                output_stream=output_stream,
                config=isort_settings.Config(
                    profile=self.config.profile
                ),  # TODO: config
                file_path=file_path,
                disregard_skip=True,
                extension=".py",
            )
            output_stream.seek(0)
            if changed:
                file_changed = True
                new_file_content = output_stream.read()

        # save for next subactions
        run_context.file_content = new_file_content

        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, new_file_content
        )
        return FormatRunResult(changed=file_changed, code=new_file_content)
