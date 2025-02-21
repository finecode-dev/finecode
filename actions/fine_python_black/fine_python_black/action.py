from __future__ import annotations

# import asyncio
# import os
from pathlib import Path
import sys
# from concurrent.futures import Executor  # ProcessPoolExecutor,
# from concurrent.futures import ThreadPoolExecutor
# from pathlib import Path

from finecode.extension_runner.actions.format import FileInfo, FormatManyRunContext, FormatManyRunPayload, FormatRunContext

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

import black
# from black import WriteBack
# from black.concurrency import schedule_formatting
from black.mode import Mode, TargetVersion
# from black.report import Report

from finecode import (
    ActionContext,
    CodeActionConfig,
    CodeFormatAction,
)
from finecode.extension_runner.actions import format as format_action
from finecode.extension_runner.interfaces import icache, ilogger


class BlackCodeActionConfig(CodeActionConfig):
    # TODO: should be set
    target_versions: list[
        # TODO: investigate why list of literals doesn't work
        # Literal["PY33", "PY34", "PY35", "PY36", "PY37",
        # "PY38", "PY39", "PY310", "PY311", "PY312"]
        str
    ] = []
    # default black line length is 88:
    # https://black.readthedocs.io/en/stable/the_black_code_style/current_style.html#line-length
    line_length: int = 88
    preview: bool = False
    unstable: bool = False
    skip_string_normalization: bool = False
    skip_source_first_line: bool = False
    skip_magic_trailing_comma: bool = False
    python_cell_magics: bool = False  # it should be a set?


class BlackCodeAction(CodeFormatAction[BlackCodeActionConfig]):
    CACHE_KEY = "BlackFormatter"

    def __init__(
        self,
        config: BlackCodeActionConfig,
        context: ActionContext,
        logger: ilogger.ILogger,
        cache: icache.ICache,
    ) -> None:
        super().__init__(config, context)
        self.logger = logger
        self.cache = cache
        self.black_mode = get_black_mode(self.config)

    @override
    async def run(
        self, payload: format_action.FormatRunPayload, run_context: FormatRunContext
    ) -> format_action.FormatRunResult:
        file_path = payload.file_path
        try:
            new_file_content = await self.cache.get_file_cache(
                file_path, self.CACHE_KEY
            )
            return format_action.FormatRunResult(changed=False, code=new_file_content)
        except icache.CacheMissException:
            pass

        file_content = run_context.file_content
        file_version = run_context.file_version
        file_changed = False
        new_file_content = file_content
        # avoid outputting low-level logs of black, our goal is to trace finecode,
        # not flake8 itself
        self.logger.disable("fine_python_black")

        # use part of `format_file_in_place` function from `black.__init__` we need to
        # format raw text.
        try:
            # `fast` whether to validate code after formatting
            # `lines` is range to format
            new_file_content = black.format_file_contents(
                file_content, fast=False, mode=self.black_mode  # , lines=lines
            )
            file_changed = True
        except black.NothingChanged:
            ...

        self.logger.enable("fine_python_black")

        # save for next subactions
        run_context.file_content = new_file_content

        await self.cache.save_file_cache(
            file_path, file_version, self.CACHE_KEY, new_file_content
        )
        return format_action.FormatRunResult(changed=file_changed, code=new_file_content)


def get_black_mode(config: BlackCodeActionConfig) -> Mode:
    return Mode(
        target_versions=set(
            [TargetVersion[ver] for ver in config.target_versions]
        ),
        line_length=config.line_length,
        is_pyi=False,
        is_ipynb=False,
        skip_source_first_line=config.skip_source_first_line,
        string_normalization=not config.skip_string_normalization,
        magic_trailing_comma=not config.skip_magic_trailing_comma,
        preview=config.preview,
        python_cell_magics=set(),  # set(python_cell_magics),
        unstable=config.unstable,
    )


class BlackManyCodeActionConfig(BlackCodeActionConfig):
    ...


class BlackManyCodeAction(CodeFormatAction[BlackCodeActionConfig]):
    CACHE_KEY = "BlackFormatter"

    def __init__(
        self,
        config: BlackManyCodeActionConfig,
        context: ActionContext,
        logger: ilogger.ILogger,
        cache: icache.ICache,
    ) -> None:
        super().__init__(config, context)
        self.logger = logger
        self.cache = cache
        self.black_mode = get_black_mode(self.config)

    @override
    async def run(
        self, payload: FormatManyRunPayload, run_context: FormatManyRunContext
    ) -> format_action.FormatRunResult:
        results_by_file_path: dict[Path, format_action.FormatRunResult] = {}
        for file_path in payload.file_paths:
            file_content, file_version = run_context.file_info_by_path[file_path]
            # TODO: avoid repetition
            try:
                new_file_content = await self.cache.get_file_cache(
                    file_path, self.CACHE_KEY
                )
                results_by_file_path[file_path] = format_action.FormatRunResult(changed=False, code=new_file_content)
                continue
            except icache.CacheMissException:
                pass

            file_changed = False
            new_file_content = file_content
            # avoid outputting low-level logs of black, our goal is to trace finecode,
            # not flake8 itself
            self.logger.disable("fine_python_black")

            # use part of `format_file_in_place` function from `black.__init__` we need to
            # format raw text.
            try:
                # `fast` whether to validate code after formatting
                # `lines` is range to format
                new_file_content = black.format_file_contents(
                    file_content, fast=False, mode=self.black_mode  # , lines=lines
                )
                file_changed = True
            except black.NothingChanged:
                ...

            self.logger.enable("fine_python_black")

            # save for next subactions
            run_context.file_info_by_path[file_path] = FileInfo(new_file_content, file_version)

            await self.cache.save_file_cache(
                file_path, file_version, self.CACHE_KEY, new_file_content
            )
            results_by_file_path[file_path] = format_action.FormatRunResult(changed=file_changed, code=new_file_content)
        
        return results_by_file_path

# def get_black_report() -> Report:
#     return Report(check=False, diff=False, quiet=True, verbose=True)


# async def run_on_many(
#     self, payload: RunOnManyPayload[FormatRunPayload]
# ) -> dict[Path, FormatRunResult]:
#     report = self.get_report()
#     with action_utils.tmp_dir_copy_path(
#         dir_path=payload.dir_path,
#         file_pathes_with_contents=[
#             (single_payload.apply_on, single_payload.apply_on_text)
#             for single_payload in payload.single_payloads
#         ],
#     ) as (_, files_pathes):
#         initial_files_version = {
#             file_path: action_utils.get_file_version(file_path)
#             for file_path in files_pathes
#         }
#         logger.disable()
#         await reformat_many(
#             sources=set(files_pathes),
#             fast=False,
#             write_back=WriteBack.YES,
#             mode=self.get_mode(),
#             report=report,
#             workers=None,
#         )
#         logger.enable()

#         result: dict[Path, FormatRunResult] = {}
#         for idx, single_payload in enumerate(payload.single_payloads):
#             if single_payload.apply_on is None:
#                 logger.error("Run on multiple supports only files")
#                 continue
#             file_path = files_pathes[idx]
#             with open(file_path, "r") as f:
#                 code = f.read()
#             # TODO: black report is generalized for all files, parse output?
#             file_changed = (
#                 action_utils.get_file_version(single_payload.apply_on)
#                 != initial_files_version[file_path]
#             )
#             result[single_payload.apply_on] = FormatRunResult(
#               changed=file_changed, code=code
#             )

#     return result

# async def reformat_many(
#     sources: set[Path],
#     fast: bool,
#     write_back: WriteBack,
#     mode: Mode,
#     report: Report,
#     workers: int | None,
# ) -> None:
#     """Reformat multiple files using a ProcessPoolExecutor.

#     This is a copy of `reformat_many` function from black. Original function expects
#     to be started outside of event loop and operates event loops of the whole program.
#     Rework to allow to run as a coroutine in another program.

#     Removed code is kept as comments to make future migrations to new version easier."""
#     # maybe_install_uvloop()

#     executor: Executor
#     if workers is None:
#         workers = int(os.environ.get("BLACK_NUM_WORKERS", 0))
#         workers = workers or os.cpu_count() or 1
#     if sys.platform == "win32":
#         # Work around https://bugs.python.org/issue26903
#         workers = min(workers, 60)
#     # TODO: process pool executor blocks for some reason execution.
#     #       Investigate why and return it
#     # try:
#     #     executor = ProcessPoolExecutor(max_workers=workers)
#     # except (ImportError, NotImplementedError, OSError):

#     # we arrive here if the underlying system does not support multi-processing
#     # like in AWS Lambda or Termux, in which case we gracefully fallback to
#     # a ThreadPoolExecutor with just a single worker (more workers would not do us
#     # any good due to the Global Interpreter Lock)
#     executor = ThreadPoolExecutor(max_workers=1)

#     # loop = asyncio.new_event_loop()
#     # asyncio.set_event_loop(loop)
#     try:
#         # loop.run_until_complete(
#         await schedule_formatting(
#             sources=sources,
#             fast=fast,
#             write_back=write_back,
#             mode=mode,
#             report=report,
#             loop=asyncio.get_running_loop(),
#             executor=executor,
#         )
#         # )
#     finally:
#         # try:
#         #     shutdown(loop)
#         # finally:
#         #     asyncio.set_event_loop(None)
#         if executor is not None:
#             executor.shutdown()
