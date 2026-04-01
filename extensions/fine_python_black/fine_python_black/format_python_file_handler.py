from __future__ import annotations

import dataclasses
import sys
from typing import cast

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

import black
from black.mode import Mode, TargetVersion

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import format_file_action
from finecode_extension_api.actions.code_quality.format_python_file_action import (
    FormatPythonFileAction,
)
from finecode_extension_api.interfaces import ilogger, iprocessexecutor


def _resolve_target_version(version: str) -> TargetVersion | None:
    normalized = version.upper().replace(".", "").replace("_", "")
    if not normalized.startswith("PY"):
        normalized = f"PY{normalized}"
    return TargetVersion.__members__.get(normalized)


def get_black_mode(config: BlackFormatFileHandlerConfig) -> Mode:
    target_versions = {
        resolved
        for version in config.target_versions
        if (resolved := _resolve_target_version(version)) is not None
    }
    return Mode(
        target_versions=target_versions,
        line_length=config.line_length,
        is_pyi=False,
        is_ipynb=False,
        skip_source_first_line=config.skip_source_first_line,
        string_normalization=not config.skip_string_normalization,
        magic_trailing_comma=not config.skip_magic_trailing_comma,
        preview=config.preview,
        python_cell_magics=set(config.python_cell_magics),
        unstable=config.unstable,
    )


@dataclasses.dataclass
class BlackFormatFileHandlerConfig(code_action.ActionHandlerConfig):
    # TODO: should be set
    target_versions: list[
        # TODO: investigate why list of literals doesn't work
        # Literal["PY33", "PY34", "PY35", "PY36", "PY37",
        # "PY38", "PY39", "PY310", "PY311", "PY312"]
        str
    ] = dataclasses.field(default_factory=list)
    # default black line length is 88:
    # https://black.readthedocs.io/en/stable/the_black_code_style/current_style.html#line-length
    line_length: int = 88
    preview: bool = False
    unstable: bool = False
    skip_string_normalization: bool = False
    skip_source_first_line: bool = False
    skip_magic_trailing_comma: bool = False
    python_cell_magics: list[str] = dataclasses.field(default_factory=list)


class BlackFormatFileHandler(
    code_action.ActionHandler[FormatPythonFileAction, BlackFormatFileHandlerConfig]
):
    def __init__(
        self,
        config: BlackFormatFileHandlerConfig,
        logger: ilogger.ILogger,
        process_executor: iprocessexecutor.IProcessExecutor,
    ) -> None:
        self.config = config
        self.logger = logger
        self.process_executor = process_executor

        self.black_mode = get_black_mode(self.config)

    @override
    async def run(
        self,
        payload: format_file_action.FormatFileRunPayload,
        run_context: format_file_action.FormatFileRunContext,
    ) -> format_file_action.FormatFileRunResult:
        file_content = run_context.file_info.file_content
        file_version = run_context.file_info.file_version

        # Avoid outputting low-level logs of black. We trace extension flow here.
        self.logger.disable("fine_python_black")
        process_result = cast(
            tuple[str, bool],
            await self.process_executor.submit(format_one, file_content, self.black_mode),
        )
        if process_result is None:
            raise code_action.ActionFailedException(
                "black formatter returned no result"
            ) from None
        new_file_content = process_result[0]
        file_changed = process_result[1]
        self.logger.enable("fine_python_black")

        # Update for next handlers in the formatting pipeline.
        run_context.file_info = format_file_action.FileInfo(new_file_content, file_version)

        return format_file_action.FormatFileRunResult(
            changed=file_changed,
            code=new_file_content,
        )


def format_one(file_content: str, black_mode: Mode) -> tuple[str, bool]:
    # use part of `format_file_in_place` function from `black.__init__` we need
    # to format raw text.
    try:
        # `fast` whether to validate code after formatting
        # `lines` is range to format
        new_file_content = black.format_file_contents(
            file_content,
            fast=False,
            mode=black_mode,  # , lines=lines
        )
        file_changed = True
    except black.NothingChanged:
        new_file_content = file_content
        file_changed = False

    return (new_file_content, file_changed)


# original multiprocess implementation:
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

#     Removed code is kept as comments to make future migrations to new version easier.
#     """
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
