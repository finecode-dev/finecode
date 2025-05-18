import inspect
import logging
from pathlib import Path

from loguru import logger

from finecode import logs
from finecode.workspace_manager import app_dirs


def init_logger(trace: bool, stdout: bool = False):
    log_dir_path = Path(app_dirs.get_app_dirs().user_log_dir)
    logger.remove()
    # disable logging raw messages
    # TODO: make configurable
    logger.configure(
        activation=[
            ("pygls.protocol.json_rpc", False),
            ("pygls.feature_manager", False),
        ]
    )
    logs.save_logs_to_file(
        file_path=log_dir_path / "execution.log",
        log_level="TRACE" if trace else "INFO",
        stdout=stdout,
    )

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
            while frame and (
                depth == 0 or frame.f_code.co_filename == logging.__file__
            ):
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
