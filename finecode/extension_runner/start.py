import inspect
import logging
import sys

from loguru import logger
from modapp.extras.logs import save_logs_to_file

import finecode.pygls_utils as pygls_utils
import finecode.extension_runner.project_dirs as project_dirs
import finecode.extension_runner.lsp_server as extension_runner_lsp
import finecode.extension_runner.global_state as global_state


async def start_runner():
    project_log_dir_path = project_dirs.get_project_dir(global_state.project_dir_path)
    logger.remove()
    # ~~extension runner communicates with workspace manager with tcp, we can print logs to stdout as well~~. See README.md
    save_logs_to_file(file_path=project_log_dir_path / 'execution.log', log_level=global_state.log_level, stdout=False)

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
    
    logger.info(f"Python executable: {sys.executable}")
    logger.info(f"Project path: {global_state.project_dir_path}")

    server = extension_runner_lsp.create_lsp_server()
    await pygls_utils.start_io_async(server)


def start_runner_sync():
    project_log_dir_path = project_dirs.get_project_dir(global_state.project_dir_path)
    logger.remove()
    # ~~extension runner communicates with workspace manager with tcp, we can print logs to stdout as well~~. See README.md
    save_logs_to_file(file_path=project_log_dir_path / 'execution.log', log_level=global_state.log_level, stdout=False)

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
    
    logger.info(f"Python executable: {sys.executable}")
    logger.info(f"Project path: {global_state.project_dir_path}")

    server = extension_runner_lsp.create_lsp_server()
    server.start_io()
