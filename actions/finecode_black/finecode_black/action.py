from __future__ import annotations
import asyncio
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
import os
from pathlib import Path
import sys

from loguru import logger

import finecode.action_utils as action_utils
from black import reformat_one, WriteBack
from black.concurrency import schedule_formatting
from black.mode import Mode, TargetVersion
from black.report import Report
from finecode import CodeFormatAction, CodeActionConfig, FormatRunResult, FormatRunPayload, RunOnManyPayload



class BlackCodeActionConfig(CodeActionConfig):
    # TODO: should be set
    target_versions: list[
        # TODO: investigate why list of literals doesn't work
        # Literal["PY33", "PY34", "PY35", "PY36", "PY37", "PY38", "PY39", "PY310", "PY311", "PY312"]
        str
    ] = []
    line_length: int = 99
    preview: bool = False
    unstable: bool = False
    skip_string_normalization: bool = False
    skip_source_first_line: bool = False
    skip_magic_trailing_comma: bool = False
    python_cell_magics: bool = False  # it should be a set?


class BlackCodeAction(CodeFormatAction[BlackCodeActionConfig]):
    async def run(self, payload: FormatRunPayload) -> FormatRunResult:
        report = self.get_report()
        # it seems like black can format only in-place, use tmp file
        with action_utils.tmp_file_copy_path(file_path=payload.apply_on, file_content=payload.apply_on_text) as file_path:
            reformat_one(
                src=file_path,
                fast=False,
                write_back=WriteBack.YES,
                mode=self.get_mode(),
                report=report,
            )
            file_changed = report.change_count > 0
            code: str | None = None
            if file_changed:
                with open(file_path, 'r') as f:
                    code = f.read()

        return FormatRunResult(changed=file_changed, code=code)

    async def run_on_many(self, payload: RunOnManyPayload[FormatRunPayload]) -> dict[Path, FormatRunResult]:
        report = self.get_report()
        with action_utils.tmp_dir_copy_path(dir_path=payload.dir_path, file_pathes_with_contents=[(single_payload.apply_on, single_payload.apply_on_text) for single_payload in payload.single_payloads]) as (dir_path, files_pathes):
            await reformat_many(
                sources=set(files_pathes),
                fast=False,
                write_back=WriteBack.YES,
                mode=self.get_mode(),
                report=report,
                workers=None,
            )
        
            result: dict[Path, FormatRunResult] = {}
            for idx, single_payload in enumerate(payload.single_payloads):
                if single_payload.apply_on is None:
                    logger.error("Run on multiple supports only files")
                    continue
                with open(files_pathes[idx], 'r') as f:
                    code = f.read()
                # TODO: black report is generalized for all files, parse output?
                result[single_payload.apply_on] = FormatRunResult(changed=True, code=code)
        
        return result

    def get_mode(self) -> Mode:
        return Mode(
            target_versions=set(
                [TargetVersion[ver] for ver in self.config.target_versions]
            ),
            line_length=self.config.line_length,
            is_pyi=False,
            is_ipynb=False,
            skip_source_first_line=self.config.skip_source_first_line,
            string_normalization=not self.config.skip_string_normalization,
            magic_trailing_comma=not self.config.skip_magic_trailing_comma,
            preview=self.config.preview,
            python_cell_magics=set(),  # set(python_cell_magics),
            unstable=self.config.unstable,
        )

    def get_report(self) -> Report:
        return Report(check=False, diff=False, quiet=True, verbose=True)


async def reformat_many(
    sources: set[Path],
    fast: bool,
    write_back: WriteBack,
    mode: Mode,
    report: Report,
    workers: int | None,
) -> None:
    """Reformat multiple files using a ProcessPoolExecutor.
    
    This is a copy of `reformat_many` function from black. Original function expects to be started
    outside of event loop and operates event loops of the whole program. Rework to allow to run as
    a coroutine in another program.
    
    Removed code is kept as comments to make future migrations to new version easier."""
    # maybe_install_uvloop()

    executor: Executor
    if workers is None:
        workers = int(os.environ.get("BLACK_NUM_WORKERS", 0))
        workers = workers or os.cpu_count() or 1
    if sys.platform == "win32":
        # Work around https://bugs.python.org/issue26903
        workers = min(workers, 60)
    try:
        executor = ProcessPoolExecutor(max_workers=workers)
    except (ImportError, NotImplementedError, OSError):
        # we arrive here if the underlying system does not support multi-processing
        # like in AWS Lambda or Termux, in which case we gracefully fallback to
        # a ThreadPoolExecutor with just a single worker (more workers would not do us
        # any good due to the Global Interpreter Lock)
        executor = ThreadPoolExecutor(max_workers=1)

    # loop = asyncio.new_event_loop()
    # asyncio.set_event_loop(loop)
    try:
        # loop.run_until_complete(
        await schedule_formatting(
            sources=sources,
            fast=fast,
            write_back=write_back,
            mode=mode,
            report=report,
            loop=asyncio.get_running_loop(),
            executor=executor,
        )
        # )
    finally:
        # try:
        #     shutdown(loop)
        # finally:
        #     asyncio.set_event_loop(None)
        if executor is not None:
            executor.shutdown()