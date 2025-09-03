from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

from finecode_extension_api import code_action
from finecode_extension_api.actions import format as format_action
from finecode_extension_api.interfaces import icache, icommandrunner, ilogger


@dataclasses.dataclass
class RuffFormatHandlerConfig(code_action.ActionHandlerConfig):
    line_length: int = 88
    indent_width: int = 4
    quote_style: str = "double"  # "double" or "single"
    target_version: str = "py38"  # minimum Python version
    preview: bool = False


class RuffFormatHandler(
    code_action.ActionHandler[format_action.FormatAction, RuffFormatHandlerConfig]
):
    CACHE_KEY = "RuffFormatter"

    def __init__(
        self,
        config: RuffFormatHandlerConfig,
        context: code_action.ActionContext,
        logger: ilogger.ILogger,
        cache: icache.ICache,
        command_runner: icommandrunner.ICommandRunner,
    ) -> None:
        self.config = config
        self.context = context
        self.logger = logger
        self.cache = cache
        self.command_runner = command_runner

        self.ruff_bin_path = Path(sys.executable).parent / "ruff"

    @override
    async def run(
        self,
        payload: format_action.FormatRunPayload,
        run_context: format_action.FormatRunContext,
    ) -> format_action.FormatRunResult:
        result_by_file_path: dict[Path, format_action.FormatRunFileResult] = {}
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

            new_file_content, file_changed = await self.format_one(
                file_path, file_content
            )

            # save for next handlers
            run_context.file_info_by_path[file_path] = format_action.FileInfo(
                new_file_content, file_version
            )

            await self.cache.save_file_cache(
                file_path, file_version, self.CACHE_KEY, new_file_content
            )
            result_by_file_path[file_path] = format_action.FormatRunFileResult(
                changed=file_changed, code=new_file_content
            )

        return format_action.FormatRunResult(result_by_file_path=result_by_file_path)

    async def format_one(self, file_path: Path, file_content: str) -> tuple[str, bool]:
        """Format a single file using ruff format"""
        # Build ruff format command
        cmd = [
            str(self.ruff_bin_path),
            "format",
            "--cache-dir",
            str(self.context.cache_dir / ".ruff_cache"),
            "--line-length",
            str(self.config.line_length),
            f'--config="indent-width={str(self.config.indent_width)}"',
            f"--config=\"format.quote-style='{self.config.quote_style}'\"",
            "--target-version",
            self.config.target_version,
            "--stdin-filename",
            str(file_path),
        ]

        if self.config.preview:
            cmd.append("--preview")

        cmd_str = " ".join(cmd)
        ruff_process = await self.command_runner.run(cmd_str)

        ruff_process.write_to_stdin(file_content)
        ruff_process.close_stdin()  # Signal EOF

        await ruff_process.wait_for_end()

        if ruff_process.get_exit_code() == 0:
            new_file_content = ruff_process.get_output()
            file_changed = new_file_content != file_content
            return new_file_content, file_changed
        else:
            raise code_action.ActionFailedException(
                f"ruff failed with code {ruff_process.get_exit_code()}: {ruff_process.get_error_output()} || {ruff_process.get_output()}"
            )
