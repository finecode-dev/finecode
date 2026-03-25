from __future__ import annotations

import dataclasses
from io import StringIO

import isort.api as isort_api
import isort.settings as isort_settings
from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_files_action
from finecode_extension_api.interfaces import icache, ilogger, iprocessexecutor
from finecode_extension_api.resource_uri import ResourceUri


@dataclasses.dataclass
class IsortFormatFilesHandlerConfig(code_action.ActionHandlerConfig):
    profile: str | None = None
    line_length: int | None = None
    multi_line_output: int | None = None
    include_trailing_comma: bool | None = None
    force_grid_wrap: int | None = None
    use_parentheses: bool | None = None
    ensure_newline_before_comments: bool | None = None
    split_on_trailing_comma: bool | None = None


class IsortFormatFilesHandler(
    code_action.ActionHandler[
        format_files_action.FormatFilesAction, IsortFormatFilesHandlerConfig
    ]
):
    def __init__(
        self,
        config: IsortFormatFilesHandlerConfig,
        logger: ilogger.ILogger,
        cache: icache.ICache,
        process_executor: iprocessexecutor.IProcessExecutor,
    ) -> None:
        self.config = config
        self.logger = logger
        self.cache = cache
        self.process_executor = process_executor

    async def run(
        self,
        payload: format_files_action.FormatFilesRunPayload,
        run_context: format_files_action.FormatFilesRunContext,
    ) -> format_files_action.FormatFilesRunResult:
        result_by_file_path: dict[ResourceUri, format_files_action.FormatRunFileResult] = {}
        for file_uri in payload.file_paths:
            file_content, file_version = run_context.file_info_by_path[file_uri]

            new_file_content, file_changed = await self.process_executor.submit(
                format_one, file_content, dataclasses.asdict(self.config)
            )

            # save for next handlers
            run_context.file_info_by_path[file_uri] = format_files_action.FileInfo(
                new_file_content, file_version
            )

            result_by_file_path[file_uri] = format_files_action.FormatRunFileResult(
                changed=file_changed, code=new_file_content
            )

        return format_files_action.FormatFilesRunResult(
            result_by_file_path=result_by_file_path
        )


def format_one(
    file_content: str, handler_config: dict[str, object]
) -> tuple[str, bool]:
    isort_config_overrides = {
        k: v for k, v in handler_config.items() if v is not None
    }

    input_stream = StringIO(file_content)
    output_stream_context = isort_api._in_memory_output_stream_context()
    with output_stream_context as output_stream:
        changed = isort_api.sort_stream(
            input_stream=input_stream,
            output_stream=output_stream,
            config=isort_settings.Config(**isort_config_overrides),
            file_path=None,
            disregard_skip=True,
            extension=".py",
        )
        output_stream.seek(0)
        if changed:
            file_changed = True
            new_file_content = output_stream.read()
        else:
            file_changed = False
            new_file_content = file_content

    return (new_file_content, file_changed)
