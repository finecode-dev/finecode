import asyncio
import inspect
import logging
import os
from pathlib import Path

import click
from loguru import logger
from modapp.extras.logs import save_logs_to_file
from modapp.extras.platformdirs import get_dirs

import finecode.pygls_utils as pygls_utils
import finecode.extension_runner.lsp_server as extension_runner_lsp


async def _start_runner(trace: bool):
    log_dir_path = Path(get_dirs(app_name='FineCode_ExtensionRunnerPy', app_author='FineCode', version='1.0').user_log_dir)
    logger.remove()
    # ~~extension runner communicates with workspace manager with tcp, we can print logs to stdout as well~~. See README.md
    save_logs_to_file(file_path=log_dir_path / 'execution.log', log_level="TRACE" if trace is True else "INFO", stdout=False)

    # pygls uses standard python logger, intercept it and pass logs to loguru
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # Get corresponding Loguru level if it exists.
            level: str | int
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find caller from where originated the logged message.
            frame, depth = inspect.currentframe(), 0
            while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    server = extension_runner_lsp.create_lsp_server()
    await pygls_utils.start_io_async(server)

@click.command()
@click.option("--trace", "trace", is_flag=True, default=False)
@click.option("--debug", "debug", is_flag=True, default=False)
def main(trace: bool, debug: bool):
    if debug is True:
        import debugpy
        # avoid debugger warnings printed to stdout, they affect I/O communication
        os.environ['PYDEVD_DISABLE_FILE_VALIDATION'] = '1'
        try:
            debugpy.listen(5681)
            debugpy.wait_for_client()
        except Exception as e:
            logger.info(e)

    asyncio.run(_start_runner(trace))


if __name__ == "__main__":
    main()
