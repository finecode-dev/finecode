import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

from finecode_extension_runner import logs


def init_logger(trace: bool, stdout: bool = False):
    venv_dir_path = Path(sys.executable) / ".." / ".."
    logs_dir_path = venv_dir_path / "logs"

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
        file_path=logs_dir_path / "workspace_manager.log",
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
