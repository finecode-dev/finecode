from __future__ import annotations

from io import StringIO
from pathlib import Path

import isort.api as isort_api
import isort.settings as isort_settings

from finecode_extension_api import code_action
from finecode_extension_api.actions import format as format_action
from finecode_extension_api.interfaces import icache, ilogger


class IsortFormatHandlerConfig(code_action.ActionHandlerConfig):
    profile: str = ""


class IsortFormatHandler(
    code_action.ActionHandler[format_action.FormatAction, IsortFormatHandlerConfig]
):
    CACHE_KEY = "Isort"

    def __init__(
        self,
        config: IsortFormatHandlerConfig,
        logger: ilogger.ILogger,
        cache: icache.ICache,
    ) -> None:
        self.config = config
        self.logger = logger
        self.cache = cache

    async def run(
        self,
        payload: format_action.FormatRunPayload,
        run_context: format_action.FormatRunContext,
    ) -> format_action.FormatRunResult:
        result_by_file_path: dict[Path, format_action.FormatRunResult] = {}
        for file_path in payload.file_paths:
            file_content, file_version = run_context.file_info_by_path[file_path]
            try:
                new_file_content = await self.cache.get_file_cache(
                    file_path, self.CACHE_KEY
                )
                result_by_file_path[file_path] = format_action.FormatRunFileResult(
                    changed=False, code=new_file_content
                )
                continue
            except icache.CacheMissException:
                pass

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
            run_context.file_info_by_path[file_path] = format_action.FileInfo(
                new_file_content, file_version
            )

            await self.cache.save_file_cache(
                file_path, file_version, self.CACHE_KEY, new_file_content
            )
            result_by_file_path[file_path] = format_action.FormatRunFileResult(
                changed=file_changed, code=new_file_content
            )

        return format_action.FormatRunResult(
            result_by_file_path=result_by_file_path
        )
